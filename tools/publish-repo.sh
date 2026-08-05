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

DAEMON_PKG=${DAEMON_PKG:-}
if [ -n "${DAEMON_PKG}" ]; then
    [ -f "${DAEMON_PKG}" ] || { echo "!!! DAEMON_PKG ${DAEMON_PKG} not found" >&2; exit 1; }
    cp "${DAEMON_PKG}" "${STAGE}/${ABI}/"
    echo "    mirroring $(basename "${DAEMON_PKG}")"
elif ! pkg info -e openvpn-auth-oauth2 2>/dev/null; then
    cat >&2 <<'EOF'
!!! the openvpn-auth-oauth2 daemon is not installed and DAEMON_PKG is unset.
    The plugin depends on it at build time and at install time, and OPNsense
    does not ship it. See "Building and publishing a release" in the README.
EOF
    exit 1
else
    echo "    note: daemon installed locally but not mirrored (set DAEMON_PKG"
    echo "          so that other firewalls can resolve the dependency)"
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
      cd ${PAGES_CLONE} && git add -A && git commit -m 'Publish ${PORTNAME}' && git push
EOF
    exit 0
fi

echo "==> publishing to gh-pages"
rm -rf "${PAGES_CLONE}"
git clone --branch gh-pages --depth 1 "${REPO_URL}" "${PAGES_CLONE}"
mkdir -p "${PAGES_CLONE}/${ABI}"
rm -f "${PAGES_CLONE}/${ABI}"/*
cp "${STAGE}/${ABI}"/* "${PAGES_CLONE}/${ABI}/"
cd "${PAGES_CLONE}"
git add -A
if git diff --cached --quiet; then
    echo "==> nothing changed"
    exit 0
fi
git commit -m "Publish $(basename "${PKGFILE}") for ${ABI}"
git push
echo "==> published; clients pick it up after 'pkg update'"
