# Maintaining os-openvpn-auth-oauth2

Notes for developing, building and publishing the plugin. Setup instructions
for firewall admins are in the [README](../README.md); how the plugin works
is in [HOW-IT-WORKS.md](HOW-IT-WORKS.md), and the design and its history in
[INVESTIGATION.md](INVESTIGATION.md).

## Development loop

Test changes without building a package. `rsync` is not in the FreeBSD base
system, so this pulls the committed tree straight onto the box with `fetch` and
`tar`, both of which are. Push your commits first: this deploys `main`, not your
working tree.

SSH in as root and drop out of tcsh first, because everything below is `sh`
syntax and an unmatched glob is a fatal error in tcsh rather than a harmless
no-op:

```bash
sh
```

Fetch and unpack over `/usr/local`, which is exactly what the package does:

```bash
fetch -o /tmp/p.tgz https://codeload.github.com/SurfHost/opnsense-plugin-entra-sso/tar.gz/refs/heads/main && tar -xf /tmp/p.tgz -C /usr/local --strip-components 3 opnsense-plugin-entra-sso-main/os-openvpn-auth-oauth2/src && rm -f /tmp/p.tgz
```

Then clear the caches and reload. OPNsense compiles Volt views and caches model,
menu and ACL data, so a stale cache is the usual reason an edit appears to do
nothing:

```bash
find /var/lib/php/cache -name '*.php' -delete; find /var/lib/php/tmp -name 'mdl_cache_*.json' -delete; rm -f /var/lib/php/tmp/opnsense_menu_cache.xml /var/lib/php/tmp/opnsense_acl_cache.json; service configd restart
```

What each change needs after that:

| Changed | Also required |
|---|---|
| model, view, controller, form | nothing further |
| `status.py` | nothing, configd runs it fresh per call |
| `supervisor.py`, `enforcement.py` | `configctl openvpnauthoauth2 restart` |
| `plugins.inc.d/openvpnauthoauth2.inc` (the pre-Apply hook) | nothing, `pluginctl` loads it on every call |
| anything under `service/templates` | `configctl template reload OPNsense/OpenVPNAuthOAuth2`, then restart |
| `login.gohtml` (the page after sign-in, one of those templates) | the template reload and restart above; after an edit to its style or script block, first run `python3 tools/csp-hash.py` on the workstation and commit the new hashes. The tool also takes a path, so `python3 tools/csp-hash.py --check /usr/local/etc/openvpn-auth-oauth2/login.gohtml` from a checkout on the box checks the copy configd rendered |
| `actions.d` | the `configd` restart above |

Useful checks on the box:

```bash
configctl openvpnauthoauth2 details
```

```bash
find /var/etc/openvpn -name 'instance-*.conf' -exec grep -H -e management-client-auth -e auth-user-pass-optional {} +
```

The SSO guard's state, and core's start record for the instance (the md5 of
the configuration on disk right after the start); replace `<uuid>` with the
instance UUID from `/usr/local/etc/openvpn-auth-oauth2/supervisor.conf`:

```bash
cat /var/run/openvpnauthoauth2.guard.json
```

```bash
cat /var/etc/openvpn/instance-<uuid>.stat
```

Run the directive repair by hand, the same code the pre-Apply hook and the
guard use (it only writes `config.xml` and never restarts anything; with
**Repair OpenVPN instance directives** off it does nothing):

```bash
pluginctl -c openvpnauthoauth2_directives
```

**Shell note:** root's login shell is tcsh, which has no `$(...)` substitution
and no `VAR=value command` prefix, and which treats a glob matching nothing as
a hard error that abandons the rest of the line. Run `sh` first, as above. Note
that `ssh root@fw '...'` still runs the remote command through tcsh no matter
what your local shell is, so wrap remote one-liners as `ssh root@fw sh -c '...'`.

**Do not use `service php_fpm restart`.** OPNsense has no php-fpm at all: the
GUI is lighttpd with mod_fastcgi spawning `php-cgi`. The command fails, and in
a chained one-liner it takes the rest of the line with it. If you ever do need
to bounce the GUI, it is `configctl webgui restart`.

## Core contract checklist

Fail-closed enforcement leans on core internals that are not a documented
interface. Re-check these against every OPNsense minor release, in
`opnsense/core` on the matching `stable/` branch:

- [ ] The `[configure]` action in `actions_openvpn.conf` still starts with
      `pluginctl -c crl;` before `ovpn_service_control.php`. If not, the
      status panel says *pre-Apply hook missing* and the log carries
      `core no longer runs pluginctl -c crl`; the guard still protects, at the
      cost of a restart when an Apply follows an instance save closely.
- [ ] `InstanceField.php` still names the files
      `/var/etc/openvpn/instance-{uuid}.conf`, `.stat` and
      `/var/run/ovpn-instance-{uuid}.pid`.
- [ ] `ovpn_service_control.php` still writes the `.stat` file, with the md5
      of the configuration, after each start.
- [ ] `OpenVPN.php` still emits every `various_flags` entry verbatim as its own
      line.
- [ ] OpenVPN's `manage.c` still fixes the management flags when the
      interface is first opened, so a running process cannot gain or lose
      `management-client-auth`.

A changed `.conf` path (in `InstanceField.php`, or in the `--config` argument
`ovpn_service_control.php` starts OpenVPN with) or `.stat` format fails
closed: an OpenVPN process whose config the guard does not recognize still
counts as the instance, so the guard can no longer verify it, stops it and,
after one repair attempt, keeps it stopped. Only a `--config` naming another
instance's `/var/etc/openvpn/instance-{uuid}.conf` makes the guard treat the
process as someone else's. A changed pidfile path does not: the guard then
sees no process at all, and the status panel reads *instance not running*
while the instance runs, so that item matters most. The hardware tests to run
after any change here, or to the guard itself, are in
[INVESTIGATION.md](INVESTIGATION.md#hardware-tests-for-fail-closed-enforcement).

## The daemon dependency

OPNsense does not build the `openvpn-auth-oauth2` port, so `pkg search
openvpn-auth-oauth2` on a firewall returns nothing and the plugin's dependency
cannot be resolved from OPNsense's own repository. FreeBSD does build it, so the
repository **mirrors FreeBSD's package** under the same name, version and
origin. Do not add `pkg.freebsd.org` to a firewall to get it: OPNsense disables
the FreeBSD repositories deliberately, and mixing them replaces OPNsense's
patched builds of `openvpn`, `unbound` and others.

Fetch the current build once, on the box you build on:

```sh
fetch -o /tmp/openvpn-auth-oauth2.pkg https://pkg.freebsd.org/$(pkg config abi)/latest/All/openvpn-auth-oauth2-1.28.0_1.pkg
```

If the filename has moved on, list what the branch currently has:

```sh
fetch -qo - https://pkg.freebsd.org/$(pkg config abi)/latest/packagesite.pkg | tar -xO -f - packagesite.yaml | grep -o '"name":"openvpn-auth-oauth2"[^}]*'
```

Install it before building, because the plugin framework resolves
`PLUGIN_DEPENDS` against the build host's package database and freezes the
resolved version into the plugin manifest:

```sh
pkg add /tmp/openvpn-auth-oauth2.pkg
```

Consequence: whenever you mirror a newer daemon, rebuild the plugin against it.

The plugin also replaces the daemon's sign-in result page with its own
`login.gohtml`, written against v1.28.0. Before mirroring a newer daemon,
compare upstream's `internal/ui/index.gohtml` and the `Execute` calls in
`internal/oauth2/handler.go` with that version: the page picks its success
branch by the exact title `Access granted`, and anything it does not
recognize falls back to a plain English page without the countdown. The
header comment in `login.gohtml` lists the data the daemon passes, and why
its inline style and script blocks must stay free of comments and of jinja
and Go template syntax.

## Building and publishing a release

Run on an OPNsense box of the **same major release** you are publishing for
(the build host's ABI is stamped into the package), from a checkout of this
repository:

```sh
DAEMON_PKG=/tmp/openvpn-auth-oauth2.pkg PUBLISH=1 ./tools/publish-repo.sh
```

That fetches the opnsense/plugins tree if needed, builds the plugin, copies the
daemon package alongside it, generates the `pkg` metadata for the current ABI,
and pushes the result to the `gh-pages` branch that GitHub Pages serves,
together with [`tools/surfhost.conf`](../tools/surfhost.conf), which is the
copy users fetch. Without `PUBLISH=1` it stages the files and prints the manual
publish commands.

[`tools/release.sh`](../tools/release.sh) wraps all of this for a tagged
version on the build box, including mirroring the current daemon build and
verifying the package before it publishes; its header has the usage.

Edits to `tools/surfhost.conf` only reach users after a publish run, and then
only once each firewall re-fetches the file: `pkg update` refreshes the
package catalogue, not the repository configuration.

Bump `PLUGIN_VERSION` in
[`os-openvpn-auth-oauth2/Makefile`](../os-openvpn-auth-oauth2/Makefile) before
building a new release, and tag the commit.

The published tree is keyed by ABI (`FreeBSD:15:amd64` for OPNsense 26.7), and
`surfhost.conf` uses `${ABI}`, so firewalls pick the right directory
automatically. An OPNsense release that moves to a new FreeBSD major needs a
fresh build published under the new ABI. Never check out the `gh-pages` branch
on Windows: those directory names contain colons, which NTFS forbids.
