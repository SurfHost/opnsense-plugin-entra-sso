# Microsoft Entra ID SSO for OpenVPN on OPNsense: Investigation

*Status: investigated August 2026, targeting OPNsense 26.7 "Xenial Xenops".*

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
  client to decide `>CLIENT:CONNECT`. The plugin adds it automatically and
  reports it in the status panel.
- Leave **Authentication** (authmode) empty, otherwise `ovpn_event.py --defer`
  *and* the SSO daemon must both approve every login (usable as a deliberate
  2-source auth, but not the default).
- Use the native **auth token lifetime / renewal** fields (`auth-gen-token`).
  Some SSO setups suggest `reneg-sec 0`, but OPNsense rejects a token lifetime
  combined with a zero Renegotiate time ("A token lifetime requires a non zero
  Renegotiate time"); keep the default `3600`. Renegotiation is satisfied
  silently by the auth token, so users aren't re-prompted mid-session.
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
| **Plugin self-heal (implemented)** | On save/apply and on service start the plugin writes the flag straight into the instance's `various_flags` node in `config.xml`, bypassing the closed OptionField, then runs `configctl openvpn restart <uuid>` so the regenerated config carries the directive. Controlled by *Repair OpenVPN instance flag* (default on). |
| Hand-edit `config.xml` | The manual version of the above; still useful for testing without the plugin enabled. |
| Exclusive mode / other injection hacks | Do not help; the directive must reach the generated instance config. |

Consequences for this plugin:

- the model deliberately does **not** validate the flag (a `performValidation`
  message is a hard error and would make the plugin un-enableable); the status
  panel reports it as a warning instead;
- `OpenVPNAuthOAuth2::ensureClientAuthFlag()` performs the repair, and
  `ServiceController` restarts the affected instance when (and only when) it
  actually added the directive; a restart drops that instance's tunnels;
- the repair writes into core's configuration section, which is why it is a
  bridge and not the destination. Two caveats: the value stays invisible to
  the OpenVPN instance form (which will drop it again on the next save there,
  triggering another repair on the next apply), and a core code path doing
  *full-model* validation on the OpenVPN model would reject the value. Core's
  own migrations do not, because `BaseModel::performValidation()` only
  validates fields that changed and migrations serialize without full-model
  validation, so the flag survives firmware updates.

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
   edit unblocks lab testing. The plugin reports the missing flag in its
   status panel.
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
     `various_flags` OptionValues** > unblocks supported use (prerequisite for
     a release; one-line model change);
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
