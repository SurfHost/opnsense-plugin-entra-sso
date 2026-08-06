#!/bin/sh
#
# Copyright (c) 2026 SurfHost.nl
# SPDX-License-Identifier: MIT
#
# Build os-openvpn-auth-oauth2 and publish it to the SurfHost package
# repository served by GitHub Pages.
#
# Run this ON an OPNsense box (FreeBSD): package building needs the FreeBSD
# toolchain, and `pkg repo` must run on the same ABI the packages target.
#
#   ./tools/publish-repo.sh              # build + stage, print next steps
#   PUBLISH=1 ./tools/publish-repo.sh    # build + stage + push to gh-pages
#
# The repository also carries the openvpn-auth-oauth2 daemon itself, because
# OPNsense does not build that port: without it, installing the plugin fails on
# an unresolvable dependency. Mirror FreeBSD's own build (same name, version and
# origin) by pointing DAEMON_PKG at a downloaded .pkg; see the README.
#
# Environment overrides:
#   PLUGINS_SRC  opnsense/plugins checkout      (default /usr/plugins)
#   STAGE        staging directory              (default /tmp/surfhost-repo)
#   PAGES_CLONE  gh-pages clone used for push   (default /tmp/surfhost-pages)
#   REPO_URL     git remote for the push        (default the GitHub HTTPS URL)
#   DAEMON_PKG   openvpn-auth-oauth2 .pkg to mirror alongside the plugin
#   RELEASE      set to 0 to skip the GitHub release step entirely
#   RELEASE_NOTES  markdown file with the release notes for that step

set -eu

CATEGORY=security
PORTNAME=openvpn-auth-oauth2
PLUGIN_SUBDIR=os-openvpn-auth-oauth2

PLUGINS_SRC=${PLUGINS_SRC:-/usr/plugins}
STAGE=${STAGE:-/tmp/surfhost-repo}
PAGES_CLONE=${PAGES_CLONE:-/tmp/surfhost-pages}
REPO_URL=${REPO_URL:-https://github.com/SurfHost/opnsense-plugin-entra-sso.git}
PUBLISH=${PUBLISH:-0}

SRC_DIR=$(cd "$(dirname "$0")/.." && pwd)
ABI=$(pkg config abi)
BUILD_DIR="${PLUGINS_SRC}/${CATEGORY}/${PORTNAME}"

echo "==> building for ABI ${ABI}"

if [ ! -d "${PLUGINS_SRC}/Mk" ]; then
    echo "    fetching the opnsense/plugins tree into ${PLUGINS_SRC}"
    opnsense-code plugins
fi

rm -rf "${BUILD_DIR}"
mkdir -p "${PLUGINS_SRC}/${CATEGORY}"
cp -R "${SRC_DIR}/${PLUGIN_SUBDIR}" "${BUILD_DIR}"

( cd "${BUILD_DIR}" && make clean >/dev/null 2>&1 || true )
( cd "${BUILD_DIR}" && make package )

PKGFILE=$(find "${BUILD_DIR}" -name 'os-*.pkg' -type f | head -n 1)
if [ -z "${PKGFILE}" ]; then
    echo "!!! no package produced under ${BUILD_DIR}" >&2
    exit 1
fi
echo "==> built $(basename "${PKGFILE}")"

echo "==> generating repository metadata in ${STAGE}/${ABI}"
rm -rf "${STAGE:?}/${ABI}"
mkdir -p "${STAGE}/${ABI}"
cp "${PKGFILE}" "${STAGE}/${ABI}/"

# The repository must always carry the daemon: publishing replaces the ABI
# directory wholesale, so omitting it would delete the dependency and break
# every install. Prefer an explicit file, otherwise regenerate one from the
# locally installed package.
DAEMON_PKG=${DAEMON_PKG:-}
if [ -n "${DAEMON_PKG}" ] && [ -f "${DAEMON_PKG}" ]; then
    cp "${DAEMON_PKG}" "${STAGE}/${ABI}/"
    echo "    mirroring $(basename "${DAEMON_PKG}")"
elif pkg info -e openvpn-auth-oauth2 2>/dev/null; then
    if [ -n "${DAEMON_PKG}" ]; then
        echo "    ${DAEMON_PKG} is gone; regenerating from the installed package"
    else
        echo "    regenerating the daemon package from the installed copy"
    fi
    pkg create -o "${STAGE}/${ABI}" openvpn-auth-oauth2
else
    cat >&2 <<EOF
!!! the openvpn-auth-oauth2 daemon is neither installed nor available as a file.
    The plugin depends on it at build time and at install time, and OPNsense
    does not ship it. Fetch it first:

      fetch -o /tmp/openvpn-auth-oauth2.pkg \\
        https://pkg.freebsd.org/${ABI}/latest/All/openvpn-auth-oauth2-1.28.0_1.pkg
      pkg add /tmp/openvpn-auth-oauth2.pkg

    See "The daemon dependency" in the README.
EOF
    exit 1
fi

pkg repo "${STAGE}/${ABI}"

if [ "${PUBLISH}" != "1" ]; then
    cat <<EOF

==> staged, not published (set PUBLISH=1 to push)

    contents of ${STAGE}/${ABI}:
$(ls -1 "${STAGE}/${ABI}" | sed 's/^/      /')

    to publish by hand:
      git clone --branch gh-pages --depth 1 ${REPO_URL} ${PAGES_CLONE}
      mkdir -p ${PAGES_CLONE}/${ABI}
      cp ${STAGE}/${ABI}/* ${PAGES_CLONE}/${ABI}/
      cp ${SRC_DIR}/tools/surfhost.conf ${PAGES_CLONE}/surfhost.conf
      cd ${PAGES_CLONE} && git add -A && git commit -m 'Publish ${PORTNAME}' && git push
EOF
    exit 0
fi

echo "==> publishing to gh-pages"
rm -rf "${PAGES_CLONE}"
git clone --branch gh-pages --depth 1 "${REPO_URL}" "${PAGES_CLONE}"
mkdir -p "${PAGES_CLONE}/${ABI}"
rm -f "${PAGES_CLONE}/${ABI:?}"/*
cp "${STAGE}/${ABI}"/* "${PAGES_CLONE}/${ABI}/"
# the repo config users fetch lives at the Pages root; publishing it from here
# keeps tools/surfhost.conf the single source of truth
cp "${SRC_DIR}/tools/surfhost.conf" "${PAGES_CLONE}/surfhost.conf"
cd "${PAGES_CLONE}"
git add -A
if git diff --cached --quiet; then
    echo "==> nothing changed"
    exit 0
fi
git commit -m "Publish $(basename "${PKGFILE}") for ${ABI}"
git push
echo "==> published; clients pick it up after 'pkg update'"

# Publishing the package repository is only half a release. GitHub's Releases
# page is fed by release objects, not by tags, so a pushed tag on its own
# leaves the sidebar advertising the previous version even though pkg is
# already serving the new one. gh is not in OPNsense's package repository, so
# on a firewall this cannot run here and prints the command instead.
[ "${RELEASE:-1}" = "1" ] || exit 0

PLUGIN_VERSION=$(sed -n 's/^PLUGIN_VERSION=[[:space:]]*\([^[:space:]]*\).*/\1/p' \
    "${SRC_DIR}/${PLUGIN_SUBDIR}/Makefile")
if [ -z "${PLUGIN_VERSION}" ]; then
    echo "!!! could not read PLUGIN_VERSION; skipping the GitHub release" >&2
    exit 0
fi
TAG="v${PLUGIN_VERSION}"
TITLE="${PLUGIN_SUBDIR} ${PLUGIN_VERSION}"

cd "${SRC_DIR}"

if ! command -v gh >/dev/null 2>&1; then
    cat <<EOF

==> the GitHub release was NOT created: gh is not installed here, and it is
    not available in OPNsense's package repository. Run this from a
    workstation that has gh, after pushing the tag:

      git push origin ${TAG}
      gh release create ${TAG} --title '${TITLE}' --notes-file <notes.md>

EOF
    exit 0
fi

if gh release view "${TAG}" >/dev/null 2>&1; then
    echo "==> GitHub release ${TAG} already exists, leaving it alone"
    exit 0
fi

# Never let gh invent the tag: it would silently cut it from the default
# branch head, which is not necessarily what was just built.
if ! git ls-remote --exit-code --tags origin "refs/tags/${TAG}" >/dev/null 2>&1; then
    echo "!!! tag ${TAG} is not on the remote; push it first:" >&2
    echo "      git push origin ${TAG}" >&2
    exit 0
fi

if [ -n "${RELEASE_NOTES:-}" ] && [ -f "${RELEASE_NOTES}" ]; then
    gh release create "${TAG}" --title "${TITLE}" --notes-file "${RELEASE_NOTES}"
    echo "==> GitHub release ${TAG} created"
else
    cat <<EOF

==> the GitHub release was NOT created: release notes are written by hand, so
    the script will not invent them. Either re-run with

      RELEASE_NOTES=notes.md PUBLISH=1 $0

    or create it now with:

      gh release create ${TAG} --title '${TITLE}' --notes-file <notes.md>

EOF
fi
