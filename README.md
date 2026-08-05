# opnsense-plugin-entra-sso

Microsoft Entra ID single sign-on for OpenVPN on OPNsense, built around
[openvpn-auth-oauth2](https://github.com/jkroepke/openvpn-auth-oauth2) and
OPNsense's new OpenVPN **Instances**.

> **Status: code complete, untested.** The supervisor (socket swap, TLS export
> from the trust store, crash backoff) and the UI (settings form, live status
> panel) are finished and the daemon config keys are verified against the
> upstream wiki (v1.28). What remains is the lab test loop below.
>
> ⚠️ **Note:** OPNsense core does not let you set the required
> `management-client-auth` directive on an OpenVPN instance (its *Various
> Flags* field is a closed list), and drops it whenever that instance is saved.
> The plugin repairs this itself on save/apply and restarts the affected
> instance; a one-line core PR remains the proper fix. See
> [the investigation](docs/INVESTIGATION.md#blocker-management-client-auth-is-not-settable-in-the-stock-ui).

## What's here

| Path | Contents |
|---|---|
| [`docs/INVESTIGATION.md`](docs/INVESTIGATION.md) | Full investigation: options considered, architecture decision, Entra ID runbook, client matrix, limitations, roadmap |
| [`os-openvpn-auth-oauth2/`](os-openvpn-auth-oauth2/) | OPNsense plugin scaffold (opnsense/plugins-style tree) |

## How it works (short version)

Users connect with a certificate-based OpenVPN profile. The server defers
authentication to the `openvpn-auth-oauth2` daemon over the management
interface; the daemon sends the client a `WEB_AUTH::` URL, the user signs in to
Entra ID in their browser (Conditional Access and MFA apply), and the daemon
approves the session. Reconnects are silent while the auth token is valid.

The plugin adds a **VPN → OpenVPN → SSO (OAuth2 / Entra ID)** page to configure
the daemon, supervises it as a proper OPNsense service, and works around the
management-socket conflict with the GUI via the daemon's pass-through proxy —
see the [architecture section](docs/INVESTIGATION.md#integration-architecture-on-opnsense).

## Requirements

- OPNsense 26.7 (OpenVPN ≥ 2.6.2, i.e. any current release)
- An Entra ID tenant + app registration
  ([runbook](docs/INVESTIGATION.md#entra-id-app-registration-runbook))
- A webauth-capable client: OpenVPN GUI ≥ 2.6, Tunnelblick ≥ 4.0.0b10,
  Viscosity, or OpenVPN3 ≥ 3.9

## Testing the scaffold on a lab box

No package build needed for a dev loop:

```sh
# on the OPNsense test box
pkg install openvpn-auth-oauth2          # from ports/pkg repo
rsync -av os-openvpn-auth-oauth2/src/ root@fw:/usr/local/
# clear MVC caches so the new page/model are picked up
ssh root@fw 'rm -rf /tmp/opnsense_mvc_cache* ; service configd restart ; service php_fpm restart'
```

Then create/select an OpenVPN instance and leave its *Authentication* empty.
Configure **VPN → OpenVPN → SSO** with your tenant/client credentials and save:
the plugin adds the `management-client-auth` directive to that instance (which
the GUI cannot set) and restarts it, so the status panel's
*management-client-auth* row should read **present**. Confirm the directive
reached the generated config:

```sh
grep management-client-auth /var/etc/openvpn/instance-*.conf
```

Re-saving that instance under **VPN → OpenVPN → Instances** silently removes
the directive again; the status row then reads *missing* until the next save
here. That round trip is worth exercising once during the lab test.

Smoke tests:

```sh
configctl template reload OPNsense/OpenVPNAuthOAuth2   # YAML renders
configctl openvpnauthoauth2 start
configctl openvpnauthoauth2 status
configctl openvpnauthoauth2 details                     # JSON health probe (feeds the UI panel)
sockstat -l | grep 9000                                 # HTTPS listener up
ls -l /var/etc/openvpn/server*.sock /var/etc/openvpn-auth-oauth2/   # socket swap done
```

Finally connect with OpenVPN GUI / Tunnelblick: the browser should open the
Entra ID login, and VPN → OpenVPN → Connection Status must still show the
session (pass-through working). Negative tests: user outside the allowed
groups is denied; pending auth times out; restarting the instance re-swaps the
socket within ~5 s.

For the release path (real package), drop `os-openvpn-auth-oauth2/` into a
checkout of [opnsense/plugins](https://github.com/opnsense/plugins) under
`security/` and run `make package` there.

## License

MIT — see [LICENSE](LICENSE).
