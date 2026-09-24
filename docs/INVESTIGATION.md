# Microsoft Entra ID SSO for OpenVPN on OPNsense: Investigation

*Status: investigated August 2026, targeting OPNsense 26.7 "Xenial Xenops".
The fail-open analysis and its fix (plugin 1.5) were added in September 2026.*

## TL;DR

True browser-based SSO (Entra ID login page, Conditional Access, MFA) for OpenVPN
on OPNsense is achievable **without patching OPNsense core** by packaging
[jkroepke/openvpn-auth-oauth2](https://github.com/jkroepke/openvpn-auth-oauth2)
as an OPNsense plugin (`os-openvpn-auth-oauth2`). The daemon implements OpenVPN's
deferred "webauth" authentication over the management interface and validates the
user against any OIDC provider; Entra ID is a first-class, documented provider.

The one genuinely hard problem is that OPNsense's new OpenVPN *Instances* already
occupy the OpenVPN management socket for GUI status. The daemon's built-in
**management pass-through proxy** plus a small **socket-swap supervisor** (shipped
in this plugin) resolves that; the details are in
[Integration architecture](#integration-architecture-on-opnsense).

The scaffold for the plugin lives in
[`../os-openvpn-auth-oauth2/`](../os-openvpn-auth-oauth2/).

## Background

- OPNsense replaced its legacy OpenVPN server pages with the MVC/API-based
  **OpenVPN > Instances** feature. Legacy OpenVPN was moved out of core into a
  deprecation plugin in 25.7; new deployments must use Instances.
- OPNsense's authentication framework (System > Access > Servers) offers Local,
  LDAP, RADIUS, TOTP and combinations. None of these can drive a browser-based
  OIDC flow, so none can deliver real Entra ID SSO with Conditional Access/MFA.
- OpenVPN 2.6 added the missing protocol piece: **deferred client authentication
  with pending authentication** (`client-pending-auth` +
  `WEB_AUTH::<url>` / `OPEN_URL:<url>` control messages), which lets a management
  client tell the VPN client "open this URL in a browser and wait". Combined with
  `auth-token`, the user gets a browser SSO login on connect and silent
  reconnects while the token is valid.

## How the webauth flow works

```
OpenVPN client            OPNsense / OpenVPN server        openvpn-auth-oauth2         Entra ID
     |  1. TLS connect (cert)     |                              |                        |
     |--------------------------->|  2. >CLIENT:CONNECT          |                        |
     |                            |----------------------------->|                        |
     |                            |  3. client-pending-auth      |                        |
     |  4. WEB_AUTH::https://...  |<-----------------------------|                        |
     |<---------------------------|                              |                        |
     |  5. user browser ------------------------------------------------> authorize ----->|
     |                            |                              |<-- 6. code callback ---|
     |                            |  7. client-auth (+ token)    |   (validates ID token, |
     |                            |<-----------------------------|    groups, issuer)     |
     |  8. tunnel up              |                              |                        |
```

The daemon runs an HTTPS listener (upstream default `:9000`; the plugin
defaults to `:9443` because OPNsense's php-fpm already occupies
`127.0.0.1:9000`) that serves the OAuth2
authorization-code callback (`/oauth2/callback`) and talks to OpenVPN through the
management interface. No password ever transits OpenVPN; identity is asserted by
Entra ID and the result is pushed to the server as `client-auth`/`client-deny`.

## Options considered

| Option | Real SSO (CA/MFA)? | Effort | Verdict |
|---|---|---|---|
| **openvpn-auth-oauth2 (OIDC webauth)** | Yes | Plugin packaging | **Chosen** |
| LDAPS via Entra Domain Services | No (password auth) | Azure infra (~€100+/mo) | Rejected |
| RADIUS bridge (NPS + Entra MFA ext., or cloud RADIUS) | Partial (push MFA, no browser/CA) | Windows/3rd-party infra | Rejected |
| ROPC (password grant) auth backend | No, blocked by MFA/CA, deprecated by Microsoft | Medium | Rejected |
| Build an SSO daemon from scratch | Yes, eventually | Months + permanent security maintenance | Rejected |

Notes on the rejected paths:

- **LDAPS / Entra Domain Services** syncs password hashes into a managed domain
  and authenticates by password, no Conditional Access evaluation at VPN login,
  significant recurring Azure cost, and password-based auth is exactly what SSO
  should remove.
- **RADIUS bridges** need extra always-on infrastructure and only bolt push-MFA
  onto password auth; Conditional Access device/location policies don't apply.
- **ROPC** is legacy, incompatible with MFA/Conditional Access, and Microsoft
  actively discourages it.
- **From scratch** would mean re-implementing three security-critical components
  (an OIDC relying party with Entra quirks, the OpenVPN management protocol with
  deferred auth, a hardened HTTPS callback server). openvpn-auth-oauth2 is MIT
  licensed, actively maintained (v1.28.x), already packaged in FreeBSD ports
  (`security/openvpn-auth-oauth2`) and battle-tested with Entra ID. The value we
  add is the OPNsense integration layer, not the SSO engine.

## Integration architecture on OPNsense

### Constraint 1: the shared-library mode is Linux-only

openvpn-auth-oauth2 offers two integration modes:

1. **Management-interface client** (default): connects to OpenVPN's management
   socket.
2. **OpenVPN plugin mode**: a Go `c-shared` shim (`.so`) loaded via a `plugin`
   directive. The upstream wiki states this **runs only under Linux**, and the
   FreeBSD port ships only the daemon binary + rc script + sample YAML, no
   shared library. Plugin mode is therefore not available on OPNsense/FreeBSD.

So we must use management-interface mode.

### Constraint 2: OPNsense owns the management socket

`opnsense/core` generates each instance config in
`src/opnsense/mvc/app/models/OPNsense/OpenVPN/OpenVPN.php`
(`generateInstanceConfig()`) and always emits `management {sockFilename} unix`.
For an **Instance** that path is defined in
`src/opnsense/mvc/app/models/OPNsense/OpenVPN/FieldTypes/InstanceField.php`:

```
management /var/etc/openvpn/instance-{uuid}.sock unix
```

> ⚠️ **Correction (verified Aug 2026):** earlier revisions of this document
> said `server{vpnid}.sock`. That form is real, but it is `OpenVPN.php`'s own
> definition for the **legacy pre-Instances servers**, not for Instances.
> Building the supervisor against it is why the first release did nothing at
> all: it waited forever on a socket that never appears.

The GUI (connection status page, session kill) talks to that socket. OpenVPN
accepts exactly one `management` directive and one connected management client,
and the model offers **no hook to inject or override directives** (the free-form
`various_flags` field only accepts bare, valueless options).

### Resolution: pass-through proxy + socket swap

openvpn-auth-oauth2 ships a **management-interface pass-through**
(`openvpn.pass-through.enabled/address/password/socket-group/socket-mode`): it
exposes its own management-compatible socket, forwards frontend commands
(`status`, `kill`, …) to the real interface, and reserves the auth commands
(`client-auth`, `client-deny`, `client-pending-auth`, `hold`, `exit`) for itself.
The OPNsense GUI only needs `status`/`kill`, which pass through cleanly.

Because core hardcodes the socket path, the plugin's supervisor performs a
**socket swap** per SSO-enabled instance:

1. OpenVPN starts and binds `S = /var/etc/openvpn/instance-{uuid}.sock`.
2. Supervisor renames `S` to `/var/etc/openvpn-auth-oauth2/instance-{uuid}.sock`.
   A rename preserves the bound unix-socket inode; OpenVPN keeps listening.
   (The swap directory deliberately sits next to `/var/etc/openvpn` so the
   rename can never cross a filesystem boundary, e.g. a tmpfs `/var/run`.)
3. Supervisor starts the daemon with
   `openvpn.addr = unix:///var/etc/openvpn-auth-oauth2/instance-{uuid}.sock`
   and
   `openvpn.pass-through.address = unix:///var/etc/openvpn/instance-{uuid}.sock`.
4. The GUI reconnects to the original path and lands on the pass-through
   listener, transparently.
5. Supervisor watches the inode of `S` (1 s poll). When OPNsense restarts the
   instance, OpenVPN re-binds `S` (clobbering the pass-through socket file); the
   supervisor detects the new inode and re-runs steps 2-3. Self-healing, no core
   patch, no core file overwritten.

**Fallback ("exclusive mode"):** if the swap misbehaves on some release, a model
toggle lets the daemon connect straight to `S`. Everything works except GUI
status/kill for that one instance (documented limitation).

### OpenVPN instance prerequisites

> ⚠️ **Correction (verified Aug 2026):** the first of these is *not* achievable
> in the stock UI. See [the blocker below](#blocker-management-client-auth-is-not-settable-in-the-stock-ui).

- Add `management-client-auth` to the instance's `various_flags` (labelled
  **Options** in the instance dialog). Required so OpenVPN asks the management
  client to decide `>CLIENT:CONNECT`. The plugin adds it automatically, keeps
  it there, and stops the instance if it ever runs without it
  ([details](#fail-open-when-the-directive-is-missing-fixed-in-15)).
- Leave **Authentication** (authmode) empty, otherwise `ovpn_event.py --defer`
  *and* the SSO daemon must both approve every login (usable as a deliberate
  2-source auth, but not the default).
- Use the native **auth token lifetime / renewal** fields (`auth-gen-token`).
  Some SSO setups suggest `reneg-sec 0`, but OPNsense rejects a token lifetime
  combined with a zero Renegotiate time ("A token lifetime requires a non zero
  Renegotiate time"); keep the default `3600`. Renegotiation is satisfied
  silently by the auth token, so users aren't re-prompted mid-session.

  > ⚠️ **Correction (verified on hardware, Aug 2026):** the native field is
  > *not* usable here. Core emits `auth-gen-token <lifetime>` without
  > `external-auth`, so OpenVPN judges the token itself and rejects it at the
  > first renegotiation ("Username/auth-token authentication failed for
  > username ''"), forcing a reconnect with a browser round-trip roughly once
  > an hour. Upstream's non-interactive refresh needs
  > `auth-gen-token <lifetime> external-auth` on the server and
  > `oauth2.refresh.use-session-id: true` in the daemon (the daemon logs
  > "detected client session ID but not configured to use it" otherwise).
  > The plugin therefore injects the full token directive itself, next to
  > `management-client-auth` in `various_flags` (core emits entries verbatim,
  > spaces included), and requires the native field to stay **empty**, since
  > two `auth-gen-token` lines stop the instance from starting.
- Server runs OpenVPN ≥ 2.6.2, satisfied by OPNsense 26.7.

### Blocker: `management-client-auth` is not settable in the stock UI

The original assessment assumed `various_flags` accepted any valueless
directive. It does not. Verified against
[`OpenVPN.xml`](https://github.com/opnsense/core/blob/master/src/opnsense/mvc/app/models/OPNsense/OpenVPN/OpenVPN.xml)
in opnsense/core: `Instances.Instance.various_flags` is a **closed
`OptionField`** whose only values are `block-ipv6`, `client-to-client`,
`duplicate-cn`, `float`, `passtos`, `persist-remote-ip`, `remote-random`,
`route-noexec`, `route-nopull`, `explicit-exit-notify` and `fast-io`.
`management-client-auth` is not among them, and the Instance model has no
free-form directive field. Without the directive OpenVPN never defers client
connects, so the daemon sits idle and no SSO happens.

The generator itself is not the problem: `generateInstanceConfig()` emits each
`various_flags` entry as a bare directive line, so the value *works* once
present in `config.xml`.

| Path | Notes |
|---|---|
| **Upstream core PR adding `management-client-auth` to the `OptionValues` list** | The real fix; one-line model change, no generator work. Prerequisite for a supportable release. |
| **Plugin self-heal (implemented)** | The plugin writes the directives straight into the instance's `various_flags` node in `config.xml`, bypassing the closed OptionField, from three places: a pre-Apply hook on core's `pluginctl -c crl` step (Apply, boot, CARP), a watcher on `config.xml` in the SSO service (after every instance save, within seconds), and the SSO page (Save, service start and restart, which restart the instance when they had to add something). The first two never restart anything. An SSO guard stops any instance process that runs without `management-client-auth` ([below](#fail-open-when-the-directive-is-missing-fixed-in-15)). Controlled by *Repair OpenVPN instance directives* (default on); with it off nothing is written, and the guard keeps an instance without the directive stopped. |
| Hand-edit `config.xml` | The manual version of the above; still useful for testing without the plugin enabled. |
| Exclusive mode / other injection hacks | Do not help; the directive must reach the generated instance config. |

Consequences for this plugin:

- the model deliberately does **not** validate the flag (a `performValidation`
  message is a hard error and would make the plugin un-enableable);
  enforcement happens at runtime, in the SSO guard, and the status panel
  reports;
- `OpenVPNAuthOAuth2::ensureClientAuthFlag()` performs the repair under the
  configuration lock, appending in a fixed order so a repeated repair yields a
  byte-identical instance config. Its callers are the SSO page (reconfigure,
  start and restart, where `ServiceController` restarts the instance when,
  and only when, something was added; that drops its tunnels), the pre-Apply
  `crl` hook, and the guard through
  `pluginctl -c openvpnauthoauth2_directives`;
- the repair writes into core's configuration section, which is why it is a
  bridge and not the destination. Two caveats: the value stays invisible to
  the OpenVPN instance form, which drops it again on every save there, and a
  core code path doing *full-model* validation on the OpenVPN model would
  reject the value. Core's own migrations do not, because
  `BaseModel::performValidation()` only validates fields that changed and
  migrations serialize without full-model validation, so the flag survives
  firmware updates;
- up to 1.4 the drop on every instance save made the instance **fail open**
  until the SSO page was saved again; the next Apply did *not* repair it. Since
  1.5 the pre-Apply hook restores the directives before core regenerates the
  configs, the watcher restores them within seconds of the save, and the
  guard stops any process that started without them anyway.

#### Fail-open when the directive is missing (fixed in 1.5)

Confirmed on hardware with 1.4: after the instance was saved in **VPN >
OpenVPN > Instances** and then applied, any client holding a valid certificate
for the instance connected without SSO, with the network access of a signed-in
user, until **Save** was pressed on the SSO page. OpenVPN does not fail closed
when the directive is missing.

Verified in OpenVPN `v2.7.7` (what 26.7.3 and later ship) and opnsense/core
`stable/26.7` (identical to tag 26.7.4 for the files cited):

- **Why it fails open.** With no `auth-user-pass-verify` script, no auth
  plugin and no `management-client-auth`, `tls_session_user_pass_enabled()` is
  false and the key becomes `KS_AUTH_TRUE` on the certificate alone
  (`ssl.c:963-972`, `2333-2342`).
- **Only `management-client-auth` decides.** `auth-user-pass-optional` without
  it stops OpenVPN from starting (`options.c:2611-2623`). The directive without
  `auth-user-pass-optional` rejects certificate-only clients
  (`ssl.c:2319-2328`). The directive with no daemon attached refuses the login
  once the deferred-auth deadline expires (`ssl_verify.c:1105-1113`). One bare
  line in the loaded config is therefore enough to call a process closed.
- **It is fixed per process.** The management object is created once
  (`init.c:4340-4346`, called at `openvpn.c:201`) and closed only after the
  SIGHUP loop (`openvpn.c:341`); its settings are applied only while
  `!ms->defined` (`manage.c:2639`, set at `2718`). Core never sends SIGHUP to
  an instance. A running process cannot gain the directive; it has to be
  restarted.
- **Start order.** Config parse (`openvpn.c:208`), daemonize and pidfile
  (`275-278`), management open (`283`), `context_init_1` (`298`), then
  `tunnel_server` (`310`), which binds the link socket. The pidfile exists
  before any client can connect, which is what the guard keys on.
- **The pre-generation hook.** Core's `[configure]` action is
  `pluginctl -c crl; ovpn_service_control.php -a configure`
  (`actions_openvpn.conf:39-43`). `plugins_configure()` runs hook tasks
  synchronously and catches only `\Error` (`plugins.inc:261-331`). Boot and
  CARP run `configctl -dq openvpn configure` (`rc.syshook.d/start/90-openvpn`,
  `carp/20-openvpn-instances`), and so does Apply
  (`OpenVPN/Api/ServiceController.php:244-254`). Per-instance start, restart
  and stop (`actions_openvpn.conf:19-37`, the Services widget in
  `openvpn.inc:81-95`) skip the `crl` step.
- **What `ovpn_service_control.php` does.** It generates the configs before
  taking any lock (`143-149`), then holds `LOCK_EX` on `.stat` (opened `a+e`,
  `162-163`, released at `200`). `ovpn_start` writes
  `{md5 of the .conf, vpnid, devname, dev_type}` to `.stat` after
  `waitforpid(..., 10)` (`59-88`). `configure` restarts an instance only when
  the md5 differs or the pid is invalid (`100-109`, `185-193`). `start` on a
  running instance regenerates the `.conf` but neither restarts it nor
  rewrites `.stat` (`62`). It does run `setup_interface()` first, though,
  which ends in `ifconfig <dev> down` (`35-57`, called at `61`, before the
  `isvalidpid()` check at `62`): it takes a running instance's tunnel
  interface down. So before its start the guard waits, for up to 30 s, while
  core holds the instance's `.stat` lock (a start, stop or configure of it is
  under way), and does not start an instance that is running again by then.
  `isvalidpid()` is `pgrep -F` (`util.inc:70-77`), which any live process
  passes, so the guard first removes a pidfile whose pid is dead or belongs
  to another program, and only while the pidfile still names that process.
  A core run that begins after these checks can still race the start.
- **Paths** (`InstanceField.php:46-49`): `/var/etc/openvpn/instance-{uuid}.conf`
  and `.stat`, and `/var/run/ovpn-instance-{uuid}.pid`.
- **How core writes the config.** Every `various_flags` entry becomes a bare
  line, in stored order (`OpenVPN.php:767-771`). The file is rewritten in
  place with touch, chmod, write (`OpenVPN.php:538`, `File.php:49-58`), so a
  reader can see it half written.
- **`explicit-exit-notify`** is a core `various_flags` option
  (`OpenVPN.xml:326`). On UDP the first SIGTERM defers the exit by 2 s while
  the server keeps serving; a second SIGTERM exits at once
  (`multi.c:3821-3873`).
- **A clean exit** removes the DCO peers and destroys the DCO interface
  (`dco_freebsd.c:309-351`), and removes the pidfile (`error.c:709`) and the
  management socket (`manage.c:2790`). A stale socket path is unlinked before
  bind (`manage.c:2041-2048`).
- **DCO keeps its own socket reference.** `if_ovpn` holds an extra reference
  on the UDP socket "until we're destroying the ifp"
  (`sys/net/if_ovpn.c:786-792`), so after a SIGKILL the kernel peers and the
  port may outlive the process until the interface is destroyed. The guard
  does that after a SIGKILL; core recreates the interface on the next start.

Package scripts are a source reading, not part of the list above; hardware
test T10 confirms them (opnsense/plugins `Mk/plugins.mk`, pkg 2.8.4
`libpkg/scripts.c` `pkg_script_run()`). A plugin's `+POST_INSTALL.post` is
appended to the generated `+POST_INSTALL`, after its configd restart and
template reload (`scripts-post`), and pkg runs that script on upgrades too,
with `PKG_UPGRADE=true`. pkg makes itself the reaper of its descendants while
the script runs and SIGKILLs every process the script leaves behind. So the
plugin's script does not restart the SSO service itself: it sends the running
supervisor SIGTERM, and daemon(8) `-R 5`, started before the update and so
outside pkg's process tree, starts the new supervisor. A script that exits 3
makes pkg exit on the spot (`pkg_script_run_child()`), so the script's
`pkill` must not pass on its exit status 3 for an empty pidfile.

The fix keeps one invariant: while SSO is enabled, the protected instance may
only run as an OpenVPN process whose loaded config provably contained a bare
`management-client-auth` line, and nothing removes the directives
automatically. The three mechanisms (pre-Apply hook, config watcher, SSO
guard), the exposure that remains and the stop sequence are described in the
README under
[Fail-closed enforcement](../README.md#fail-closed-enforcement). How the guard
proves a process: it reads the `.conf` until two reads agree, requires the
bare directive, and compares the md5 with core's `.stat` written after this
start (read under a non-blocking `LOCK_SH`, and only when it is newer than the
pidfile). The `.stat` md5 is that of the `.conf` on disk right after the
start, not necessarily the one OpenVPN parsed: a `.conf` rewritten after the
pidfile but before the `.stat` write counts as unverified (modification
times), while one rewritten between OpenVPN's config parse and its pidfile
write cannot be told apart (see [Known limitations](#known-limitations)).
The verdict is kept per process identity
(pid, pidfile inode, pidfile mtime) in `/var/run/openvpnauthoauth2.guard.json`,
which is sound because of the per-process rule above.

Rejected alternatives:

| Alternative | Why not |
|---|---|
| An **Authentication** backend as a backstop | OpenVPN ANDs its backends (`ssl_verify.c:1786-1795`), so every login would need both, which breaks SSO |
| `username-as-common-name` as a tripwire | blanks the common name, and unticking it in the GUI disarms it |
| `management-hold` | stripped together with our directives on every save, and the daemon releases the hold automatically |
| Watching for `ESTABLISHED` and sending `client-kill` | acts after the client is already in |
| Patching core's generated `.conf` at runtime under the `.stat` lock | edits a core file and depends on core's lock ordering |
| A `config` syshook to re-add the directives | asynchronous and can be dropped; the watcher covers the same case |
| An opt-out switch for the guard | recreates this exact bug from a checkbox |
| Removing the directives automatically on disable | silently turns a protected instance into a certificate-only one; documented as a manual step instead |

#### Guard log lines

All lines use the syslog tag `openvpn-auth-oauth2`; the guard's lines start
with `SSO guard: `.

| ID | Level | Text |
|---|---|---|
| P1 | notice | `restored the SSO directives on OpenVPN instance {uuid} before OpenVPN applied its configuration` |
| P2 | notice | `restored the SSO directives on OpenVPN instance {uuid} for the SSO guard` |
| P3 | err | `could not restore the SSO directives on OpenVPN instance {uuid}: {message}` |
| G1 | notice | `watching OpenVPN instance {uuid} (repair on\|off)` |
| G2 | notice | `OpenVPN instance {uuid} pid {pid} enforces SSO (management-client-auth loaded, start record md5 {md5})` |
| G3 | notice | `waiting for core to record the start of OpenVPN instance {uuid} pid {pid} ({reason})` |
| G4 | alert | `OpenVPN instance {uuid} pid {pid} is running WITHOUT management-client-auth; any valid client certificate connects without SSO. Stopping it now` |
| G5 | err | `cannot prove OpenVPN instance {uuid} pid {pid} loaded management-client-auth ({reason}); stopping it` |
| G6 | notice | `stopped OpenVPN instance {uuid} pid {pid} after {t}s; every session it admitted was dropped` |
| G7 | err | `pid {pid} ignored SIGTERM for 5s; sent SIGKILL` |
| G8 | notice | `destroyed DCO interface {devname} so no kernel peer outlives the stopped process` |
| G9 | notice | `restoring the SSO directives in config.xml (config.xml lost them\|before restarting the stopped instance)` |
| G10 | notice | `starting OpenVPN instance {uuid} with the SSO directives (repair {n}/3 in 15 minutes)` |
| G11 | alert | `keeping OpenVPN instance {uuid} stopped: {reason}. {what to do}` |
| G12 | warning | `OpenVPN instance {uuid} started again without SSO enforcement and was stopped ({n} times since {time})`, at most once a minute |
| G13 | warning | `the saved configuration of OpenVPN instance {uuid} lacks management-client-auth and repair is off; the next start of this instance will be stopped` |
| G14 | warning | `core no longer runs pluginctl -c crl before regenerating OpenVPN configs; an Apply may start the instance without management-client-auth until the guard stops it` |
| G16 | alert | `internal error, guard keeps running: {traceback}`, at most once a minute per guard step (reap, config watch, instance check); a failing step does not skip the others, and while the instance check fails no heartbeat is written, so the status panel shows *not watched* within 15 s |
| G17 | warning | `stopped; OpenVPN instance {uuid} is no longer watched` |
| G18 | err | `could not stop OpenVPN instance {uuid} pid {pid}: it outlived SIGKILL; trying again every 5s`; the instance is then held (G11) and the stop is retried, logged at most once a minute |
| G19 | notice | `OpenVPN instance {uuid} was started again (pid {pid}) while the SSO directives were being restored; checking that process instead of starting the instance`; replaces G10 and counts as one of the three repairs in 15 minutes |
| G20 | notice | `resuming the repair of OpenVPN instance {uuid} that a restart of the SSO service interrupted`; the first check after a restart found no process of the instance while the last state written, less than 60 s before, was *stopping* or *repairing*; G9 follows |
| M1 | notice | `OpenVPN instance {uuid} is held stopped by the SSO guard; SSO daemon paused` |
| M2 | err | `SSO guard thread stopped unexpectedly; restarting the supervisor` |

Core's own start line, `OpenVPN server {vpnid} instance started on PID {pid}.`,
is logged under the tag `openvpn`.

#### Hardware tests for fail-closed enforcement

Run these after any change to the guard, the hook or the model, and after a
core minor update (see the core contract checklist in the README's maintainer
notes). Run `sh` first; root's tcsh cannot parse the setup.

Setup, in a second SSH session:

```sh
U=$(sed -n 's/^instance_uuid=//p' /usr/local/etc/openvpn-auth-oauth2/supervisor.conf)
C=/var/etc/openvpn/instance-$U.conf; P=/var/run/ovpn-instance-$U.pid; S=/var/etc/openvpn/instance-$U.stat
check() { echo "pid $(cat $P 2>/dev/null)"; grep -cx management-client-auth $C; cat $S; echo; cat /var/run/openvpnauthoauth2.guard.json; echo; }
tail -F /var/log/system/latest.log /var/log/openvpn/latest.log
```

Test client: a certificate-only client in a loop, for example
`openvpn --config test.ovpn --connect-retry 1 1`, watching for
`Initialization Sequence Completed`. With SSO in force it waits for the
browser; without SSO it completes at once.

- **T0, baseline.** Deploy, run `configctl openvpnauthoauth2 restart`. Expect
  G1 then G2, the status row *enforced*, and `check` printing `1` and a guard
  state `enforced`. Confirm with `ps -ww -o comm= -o args= -p $(cat $P)` that the
  comm is `openvpn` and the args contain the `.conf` path, and note
  `sysctl vfs.timestamp_precision`.
- **T1, the reported bug path.** Save the instance without changes, do not
  Apply. Within a few seconds expect G9 (*config.xml lost them*) and P2; the
  saved row goes amber, then green. Press Apply. Pass: no new
  `instance started on PID` line, `cat $P` unchanged, directive present.
- **T1b, the pre-Apply hook alone.** `configctl openvpnauthoauth2 stop`, save
  the instance, Apply. Expect P1 and no restart line (pid unchanged); the
  enforcement row shows *not watched*. Then
  `configctl openvpnauthoauth2 start`.
- **T2, open instance plus guard repair.** Stop the SSO service, save the
  instance, run `configctl openvpn restart $U`: `grep -cx
  management-client-auth $C` now prints `0` and the test client connects
  without a browser, the bug reproduced. Run
  `configctl openvpnauthoauth2 start`. Expect, within about a second: G1, G4,
  G6 (G7 and G8 only if SIGKILL was needed), G9 and P2, G10, core's
  `started on PID`, then G2. Pass: the client is dropped and gets back in
  only through the browser; `check` shows stops 1 and repairs 1.
- **T3, GUI race.** Pre-type `configctl openvpn restart $U`, save the
  instance and press Enter at once; repeat five times. Pass: either G9 then
  G2 (the watcher won), or G4, G6, G10, G2. Never G2 for a pid whose `.conf`
  lacked the directive, and the client loop never completes without a browser
  login.
- **T4, repair off and hold.** Untick *Repair OpenVPN instance directives*
  and Save; expect G1 *(repair off)*. Save the instance, then Apply: expect
  G13, then after the Apply a core start, G4, G6 and G11. Pass: `cat $P`
  fails, status *instance kept stopped*, the client times out. Start the
  instance on **System > Diagnostics > Services**: a start line, then G6 and
  G12 (both at most once a minute while held); it stays stopped. Tick
  Repair and Save: expect the controller line *repaired ... and restarted
  it* and G2; the hold is cleared.
- **T5, `explicit-exit-notify`.** Add it to the instance Options (UDP
  instance) and Apply; the guard restores the directive, one extra bounce is
  acceptable here. Repeat T2. Pass: G6 reports under a second, not 2 s or
  more.
- **T6, supervisor crash.** `pkill -9 -f OpenVPNAuthOAuth2/supervisor.py`.
  Pass: G1 after about 5 s, no G4 or G5, pid unchanged.
- **T7, cached verdict.** On a box whose other instances have no pending
  changes, note `cat $P`, run `configctl openvpn configure`, which
  regenerates the `.conf` but restarts an instance only when its md5 changed
  or its pid is invalid (`ovpn_service_control.php:190-193`), then
  `configctl openvpnauthoauth2 restart`. Pass: G1 then G2 (persisted
  verdict), no G3, G4, G5 or G10, and `cat $P` prints the same pid. Do not
  use `configctl openvpn start $U` here: on a running instance it takes the
  tunnel interface down.
- **T8, token field.** Set the instance's **Auth Token Lifetime**, Save and
  Apply. Pass: G2, enforcement green, token row amber. Clear the field
  afterwards.
- **T9, DCO only.** `ifconfig -g openvpn` before and after T2. The interface
  disappears only when the process was SIGKILLed (G8), and core recreates it
  on start.
- **T10, update.** On a box running 1.4 with SSO enabled, note
  `cat /var/run/openvpnauthoauth2.child.pid`, then update on **System >
  Firmware > Updates**; the update log shows
  `Restarting the openvpn-auth-oauth2 supervisor`. Pass: about 5 s after the
  update a new pid in that file, G1 in the log, `pgrep -f configd.py` still
  running, and the enforcement row no longer *not watched*. Repeat on 1.5
  with `pkg install -f os-openvpn-auth-oauth2`: the same, and no instance
  restart.

Open measurements: the time from pidfile to SIGTERM against OpenVPN's own
start-up plus a client handshake (T2, T3), and whether `vfs.timestamp_precision`
gives sub-second mtimes (T0), which the concurrent-generation check in the
guard relies on.

## Entra ID app registration (runbook)

1. **Entra admin center > App registrations > New registration**
   - Name e.g. `OPNsense OpenVPN SSO`; single tenant.
   - Platform **Web**; redirect URI: `https://<baseUrl>/oauth2/callback`
     (e.g. `https://vpn.example.nl:9443/oauth2/callback`).
2. **Certificates & secrets** > new client secret (confidential client).
   Record the *value* immediately; set a calendar reminder for expiry,
   Entra secrets max out at 24 months and the VPN dies silently when it lapses.
3. **API permissions**: `openid`, `profile`, `offline_access` (delegated;
   admin consent recommended).
4. **Token configuration > Add groups claim**: security groups, emit as group
   **object IDs** in the ID token. The plugin's *allowed groups* field matches
   these IDs (`oauth2.validate.groups`).
   - ⚠️ **Groups overage**: if a user is in >200 groups, Entra omits the claim
     and group validation fails closed. Mitigate by "Groups assigned to the
     application" in the claim config and assigning the app, or use App Roles.
5. Optional hardening: **Enterprise application > Assignment required**, plus a
   **Conditional Access** policy scoped to this app (require MFA / compliant
   device / named locations).
6. Issuer used by the plugin:
   `https://login.microsoftonline.com/<tenant-id>/v2.0`.

## Networking and TLS exposure

The callback listener must be reachable *by the user's browser* (not by the VPN
client, since the browser flow happens outside the tunnel):

| Option | Notes |
|---|---|
| **Dedicated HTTPS listener on :9443 with a cert from the OPNsense trust store** | Recommended; simplest; Viscosity requires HTTPS |
| Reverse proxy via os-caddy / os-nginx | Nice if a proxy already terminates TLS on 443 |
| Plain HTTP | Only for lab testing; several clients refuse it |

Requirements:

- DNS name of `baseUrl` must resolve publicly and match the certificate
  (a Let's Encrypt cert via the os-acme-client plugin works well).
- Firewall: WAN rule allowing TCP/9443 to the firewall itself (or restrict to
  expected user networks). The listener speaks only OAuth2 endpoints and is
  protected by state cookies (`http.secret`), but it is still an exposed
  service, keep the port filtered where possible.
- On HA/CARP pairs the daemon holds in-memory state; run it on both nodes,
  expect re-auth after failover.

## Client support matrix (upstream, Aug 2026)

| Client | Webauth SSO | Notes |
|---|---|---|
| OpenVPN GUI (Windows) ≥ 2.6 | ✅ | opens default browser |
| Tunnelblick (macOS) ≥ 4.0.0b10 | ✅ | |
| OpenVPN3 / openvpn3-linux ≥ 3.9 | ✅ | |
| Viscosity | ✅ | HTTPS `baseUrl` required |
| OpenVPN Connect v3 | ⚠️ partial | upstream documents workaround |
| NetworkManager (GNOME) | ❌ | no webauth support |

Client profile: certificate-based profile exported from OPNsense, plus
`auth-retry interact`; no `auth-user-pass` needed.

## Known limitations

1. **The socket swap is a workaround.** It is self-healing and touches no core
   files, but it depends on FreeBSD unix-socket rename semantics and on the GUI
   reconnecting per query (it does, status polls open a fresh connection).
   Roadmap item: upstream a core option for a configurable management socket
   path per instance, which would delete the hack.
2. **One SSO instance in v1.** One daemon = one HTTP listener + one management
   connection. Multi-instance needs per-instance daemon configs and ports,
   the model is shaped to allow that later.
3. **`management-client-auth` cannot be set through the stock UI at all**,
   core's `various_flags` is a closed OptionField
   ([details](#blocker-management-client-auth-is-not-settable-in-the-stock-ui)).
   A core PR is the prerequisite for a supportable release; a `config.xml`
   edit unblocks lab testing. Since 1.5 the plugin keeps the directives on
   the instance and stops the instance if it runs without them
   ([details](#fail-open-when-the-directive-is-missing-fixed-in-15)). What
   remains:
   - a per-instance restart (dashboard, Services page, Connection Status, a
     cron job, an HA sync) within a second or two of an instance save, with
     repair off, or after core stops writing the directive, starts the
     instance open until the guard stops it, within about a second; only a
     holder of a valid, unrevoked client certificate from the instance CA can
     use that window, and no auth token is issued in it;
   - with the SSO service stopped by hand while SSO is enabled, per-instance
     restarts are unwatched; Apply, boot and CARP stay covered by the
     pre-Apply hook, and the status panel shows *not watched*;
   - after a restart of the SSO service there are a few seconds without a
     guard before its startup check, which also takes up a repair that the
     restart interrupted. An update restarts a running SSO service the same
     way, through the package's post-install script; should that not take,
     the old version keeps running until the service is restarted by hand
     (README 3.3), coming from 1.4 without a guard and with the status panel
     showing *not watched*;
   - a race of a few milliseconds the guard cannot see: when two core runs
     overlap (an Apply during a per-instance restart) and the second
     regenerates the `.conf` between OpenVPN's config parse and its pidfile
     write, `.stat` records the md5 of the regenerated file, which also
     predates the pidfile, so a process that parsed a config without the
     directive passes as enforcing until it exits. It takes two core runs
     within milliseconds of each other and a config that gained the
     directive in between;
   - the `pluginctl -c crl` step the pre-Apply hook relies on is verified in
     source but is not a documented core interface; the status panel and log
     line G14 report its disappearance;
   - on CARP pairs each node needs the plugin enabled, since its settings are
     not part of the XMLRPC sync;
   - disabling or removing the plugin, or selecting another instance, leaves
     the directives on the old instance, which then refuses every client after
     about 60 seconds. This is documented, never automated.
4. **Client coverage** excludes NetworkManager and is partial for Connect v3.
5. **Secret lifecycle**: Entra client secrets expire (max 24 months).

## Open items to verify during implementation

- [ ] `security/openvpn-auth-oauth2` is present/buildable in the **opnsense/ports**
      fork (it is in FreeBSD ports; if absent, step 0 is a sync PR).
- [x] The `various_flags` validation mask in the core OpenVPN model accepts
      `management-client-auth`. **Resolved: it does NOT**, closed OptionField,
      see [the blocker section](#blocker-management-client-auth-is-not-settable-in-the-stock-ui).
- [ ] Unix-socket rename swap on FreeBSD 14 behaves as described (10-line test
      on a lab box) and OpenVPN's behaviour on management re-bind.
- [x] Exact `oauth2.provider` key for Entra in the daemon config (`generic` vs
      a dedicated Azure provider id) and minimal scopes.
      **Resolved (Aug 2026, upstream wiki v1.28):** Entra ID uses the default
      `generic` provider (no dedicated id); `openid profile` are default
      scopes, `offline_access` is needed for refresh. Further key corrections
      applied to the template: TLS files are `http.cert`/`http.key`,
      browser-vs-VPN IP matching is `http.check.ipaddr` (not
      `oauth2.validate.ipaddr`), there is no `oauth2.validate.common-name`,
      and `oauth2.refresh.enabled` requires an `oauth2.refresh.secret`.

## Roadmap

1. Finish the scaffold into a working plugin; test on a 26.7 lab box
   (procedure in the [README](../README.md)), using the `config.xml` workaround
   for `management-client-auth`.
2. Upstream contributions, in order of leverage:
   - opnsense/core: **add `management-client-auth` to the instance
     `various_flags` OptionValues**, or a plugin hook before
     `generateInstanceConfig()` in `ovpn_service_control.php` > unblocks
     supported use (prerequisite for a release; the first is a one-line model
     change). Either retires the pre-Apply hook and the config watcher, not
     the SSO guard, which stays as the check on what the running process
     actually loaded;
   - opnsense/core: configurable management socket path (or post-start hook)
     for instances > removes the socket swap;
   - FreeBSD build of the `.so` shim (Go c-shared on freebsd/amd64) > removes
     the management-mode complexity entirely;
   - the plugin itself to opnsense/plugins once stable.

## References

- openvpn-auth-oauth2: <https://github.com/jkroepke/openvpn-auth-oauth2> (README + wiki:
  Configuration, Providers/Entra, Supported clients, Management pass-through)
- FreeBSD port: <https://www.freshports.org/security/openvpn-auth-oauth2/>
- OPNsense OpenVPN Instances model (config generator, `management` directive):
  <https://github.com/opnsense/core/blob/master/src/opnsense/mvc/app/models/OPNsense/OpenVPN/OpenVPN.php>
- OPNsense OpenVPN Instances socket naming (`sockFilename`, unchanged since 23.7):
  <https://github.com/opnsense/core/blob/master/src/opnsense/mvc/app/models/OPNsense/OpenVPN/FieldTypes/InstanceField.php>
- OPNsense plugin development: <https://docs.opnsense.org/development/examples/helloworld.html>
- OPNsense 25.7 release notes (legacy OpenVPN deprecation):
  <https://docs.opnsense.org/releases/CE_25.7.html>
- OpenVPN management interface / pending auth:
  <https://github.com/OpenVPN/openvpn/blob/master/doc/management-notes.txt>
- Entra ID groups claim & overage:
  <https://learn.microsoft.com/en-us/entra/identity-platform/optional-claims>
