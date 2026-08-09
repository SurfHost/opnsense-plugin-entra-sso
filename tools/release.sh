#!/bin/sh
#
# Copyright (c) 2026 SurfHost.nl
# SPDX-License-Identifier: MIT
#
# One-shot release driver for the OPNsense build box. Fetch this file
# standalone, give it the version, and it does everything up to the push:
#
#   fetch -o /root/release.sh https://raw.githubusercontent.com/SurfHost/opnsense-plugin-entra-sso/main/tools/release.sh
#   sh /root/release.sh 1.4
#
# It clones the v<version> tag, mirrors the current FreeBSD build of the
# daemon (version discovered from the branch metadata, so a rolled version
# never 404s), builds and stages, verifies the package contents and the
# frozen daemon dependency, and only then publishes. The single interactive
# moment is the gh-pages push: enter SurfHost and paste a fine-grained PAT
# (Contents: write, this repo only) as the password, and revoke the token
# afterwards. The GitHub release itself is created from a workstation, since
# gh is not available on OPNsense.

set -eu

VERSION=${1:?usage: sh release.sh <version, e.g. 1.4>}
TAG="v${VERSION}"
REPO=https://github.com/SurfHost/opnsense-plugin-entra-sso.git
CHECKOUT=/root/entra-sso
DAEMON_PKG=/root/openvpn-auth-oauth2.pkg
STAGE=${STAGE:-/tmp/surfhost-repo}
PAGES=https://surfhost.github.io/opnsense-plugin-entra-sso

ABI=$(pkg config abi)
echo "==> releasing ${TAG} for ${ABI}"

pkg install -y git

# the tag must exist before anything is built from it
if ! git ls-remote --exit-code --tags "${REPO}" "refs/tags/${TAG}" >/dev/null 2>&1; then
    echo "!!! tag ${TAG} is not on GitHub; push it from the workstation first:" >&2
    echo "      git tag ${TAG} && git push origin ${TAG}" >&2
    exit 1
fi

rm -rf "${CHECKOUT}"
git clone --depth 1 --branch "${TAG}" "${REPO}" "${CHECKOUT}"

# refuse to ship a tag whose Makefile disagrees with the requested version
STAMPED=$(sed -n 's/^PLUGIN_VERSION=[[:space:]]*\([^[:space:]]*\).*/\1/p' \
    "${CHECKOUT}/os-openvpn-auth-oauth2/Makefile")
if [ "${STAMPED}" != "${VERSION}" ]; then
    echo "!!! tag ${TAG} carries PLUGIN_VERSION=${STAMPED}, not ${VERSION}" >&2
    echo "    bump the Makefile, commit, retag, and run again" >&2
    exit 1
fi

# mirror the current FreeBSD build of the daemon
DAEMON_VERSION=$(fetch -qo - "https://pkg.freebsd.org/${ABI}/latest/packagesite.pkg" \
    | tar -xO -f - packagesite.yaml \
    | grep '"name":"openvpn-auth-oauth2"' \
    | sed -n 's/.*"version":"\([^"]*\)".*/\1/p' | head -n 1)
if [ -z "${DAEMON_VERSION}" ]; then
    echo "!!! openvpn-auth-oauth2 not found in ${ABI}/latest on pkg.freebsd.org" >&2
    exit 1
fi
echo "==> mirroring daemon ${DAEMON_VERSION}"
fetch -o "${DAEMON_PKG}" \
    "https://pkg.freebsd.org/${ABI}/latest/All/openvpn-auth-oauth2-${DAEMON_VERSION}.pkg"
# -f aligns the build host's package database with the mirrored file, since
# PLUGIN_DEPENDS freezes the installed version into the plugin manifest
pkg add -f "${DAEMON_PKG}"

echo "==> stage build (no credentials involved yet)"
( cd "${CHECKOUT}" && env DAEMON_PKG="${DAEMON_PKG}" STAGE="${STAGE}" sh ./tools/publish-repo.sh )

PLUGIN_PKG="${STAGE}/${ABI}/os-openvpn-auth-oauth2-${VERSION}.pkg"
if [ ! -f "${PLUGIN_PKG}" ]; then
    echo "!!! expected ${PLUGIN_PKG}, staged instead:" >&2
    ls -1 "${STAGE}/${ABI}" >&2
    exit 1
fi

echo "==> verifying package contents"
FILES=$(pkg info -F "${PLUGIN_PKG}" -l)
echo "${FILES}" | grep -q 'rc\.d/openvpnauthoauth2' \
    || { echo '!!! rc script missing from the package' >&2; exit 1; }
echo "${FILES}" | grep -q 'supervisor\.py' \
    || { echo '!!! supervisor.py missing from the package' >&2; exit 1; }
if echo "${FILES}" | grep -Eq '__pycache__|\.pyc'; then
    echo '!!! stray Python bytecode in the package (plist ships everything in the tree)' >&2
    exit 1
fi

# a mismatch installs cleanly and fails at runtime, the worst failure mode
if ! pkg query -F "${PLUGIN_PKG}" '%dn %dv' \
    | grep -qx "openvpn-auth-oauth2 ${DAEMON_VERSION}"; then
    echo "!!! manifest dependency does not match the mirrored daemon ${DAEMON_VERSION}:" >&2
    pkg query -F "${PLUGIN_PKG}" '%dn %dv' >&2
    exit 1
fi
echo "==> verified: files ok, dependency openvpn-auth-oauth2 ${DAEMON_VERSION}"

git config --global user.name >/dev/null 2>&1 || git config --global user.name "SurfHost"
git config --global user.email >/dev/null 2>&1 || git config --global user.email "hans@surfhost.nl"

echo "==> publishing: enter SurfHost and paste the PAT at the git prompt"
( cd "${CHECKOUT}" && env DAEMON_PKG="${DAEMON_PKG}" STAGE="${STAGE}" PUBLISH=1 sh ./tools/publish-repo.sh )

echo "==> surfhost.conf as served by Pages:"
fetch -qo - "${PAGES}/surfhost.conf" || echo '    (fetch failed, Pages may still be deploying)'
echo "==> plugin version according to the published repository:"
( fetch -qo - "${PAGES}/${ABI}/packagesite.pkg" | tar -xO -f - packagesite.yaml \
    | grep -o '"name":"os-openvpn-auth-oauth2","origin":"[^"]*","version":"[^"]*"' ) \
    || echo '    (not visible yet; Pages deploys lag a minute or two, re-run the fetch to re-check)'

# double quotes on purpose: the command is pasted into cmd.exe on the
# workstation, and cmd does not treat single quotes as quoting
echo "==> done. Remaining, from the workstation:"
echo "      gh release create ${TAG} --title \"os-openvpn-auth-oauth2 ${VERSION}\" --notes-file notes.md"
