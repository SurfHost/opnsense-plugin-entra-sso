# How it works

Background to the [README](../README.md): what the plugin does behind the setup
steps, and how it keeps SSO enforced. The design, the rejected alternatives and
the known limitations are in [INVESTIGATION.md](INVESTIGATION.md); build and
release notes are in [MAINTAINING.md](MAINTAINING.md).

## Overview

Users connect with a certificate-based profile. The OpenVPN server defers
authentication to the `openvpn-auth-oauth2` daemon over its management
interface; the daemon sends the client a `WEB_AUTH::` URL, the user signs in to
Entra ID in a browser, and the daemon approves the session with an auth token
that makes later reconnects silent.

OPNsense's OpenVPN instances already use the management socket for the GUI's
Connection Status page, and OpenVPN allows only one management client. The
plugin resolves that with a socket swap plus the daemon's pass-through proxy, so
both the SSO daemon and the GUI keep working. The full design is in
[INVESTIGATION.md](INVESTIGATION.md#integration-architecture-on-opnsense).

The exported profile carries **no** `auth-user-pass` line, and that is correct.
The plugin puts the matching `auth-user-pass-optional` directive on the server,
so OpenVPN accepts a certificate-only client and hands the decision to the SSO
daemon. Without `auth-user-pass-optional` the server rejects the client during
TLS negotiation with *"Auth Username/Password was not provided by peer"*, and
no browser ever opens.

### Silent reconnects and token renewal

Reconnecting and the hourly renegotiation happen without a browser prompt: the
SSO daemon renews the auth token in place, falling back to its stored Entra
refresh token when needed. The browser only returns when the daemon cannot
renew you at all, mainly after a restart of the SSO service (its session store
is in memory) or when Entra revokes the session.

OpenVPN renegotiates the session keys roughly hourly, and the plugin injects an
`auth-gen-token <lifetime> external-auth` directive into the instance (the
lifetime is configurable under the SSO page's advanced settings, default 24
hours) so the SSO daemon renews the auth token silently at each renegotiation,
without a browser. This only works while the instance's **Auth Token Lifetime**
field stays empty: a value there emits a second `auth-gen-token` line, so the
plugin skips its injection to keep the instance bootable, and every
renegotiation then falls back to a reconnect with a browser round-trip. The
status panel's *Silent token renewal* row then reads *instance's Auth Token
Lifetime in use*. Why the native field cannot be used is explained in
[INVESTIGATION.md](INVESTIGATION.md#openvpn-instance-prerequisites).

### The page after sign-in

The browser page at the end of a sign-in comes from the plugin, not from the
daemon's built-in page: the daemon config sets `http.template` to
`/usr/local/etc/openvpn-auth-oauth2/login.gohtml`, a Go template that configd
renders next to the daemon config, from `login.gohtml` in the plugin's
configd templates. The daemon reads it only when it starts, so **Save** on
the SSO page renders it again and restarts the daemon, and the daemon does
not start at all when the file is missing or does not parse.

- **Logo**: optional, set under **Page after sign-in** on the SSO page. The
  page shows it at the top of the card in every state, always on white: the
  card is white in light mode and in dark mode the logo sits on a white
  plate, so use a logo made for a light background. It is stored in
  config.xml as a data URI (`data:image/...;base64,...`, at most 64 KB of
  image) and written into the rendered page, so the page needs no extra
  request for it. The data URI is about a third larger than the file, up to
  about 87 KB, and each config backup (**System > Configuration > History**)
  holds a copy of it too. The model accepts only PNG, JPEG, SVG and WebP in
  base64, and the template checks that again, so no markup or Go template
  action can reach the page. The page's Content-Security-Policy allows it
  with `img-src 'self' data:`.
- **Success**: *Toegang verleend* and *Deze pagina zal automatisch sluiten in
  10*, counting down to 1 with a thin bar that runs out alongside. At 0 the
  page asks the browser to close it.
- **Failure**: *Toegang geweigerd*, *Neem contact op met uw beheerder.* and a
  *Fout-ID*, without a countdown. The ID is the `error_id` of the daemon's log
  line that holds the actual reason, so search the log for it.
- The text is Dutch. When the browser's first language is not Dutch, a script
  on the page switches it to English. The daemon's own translations for
  German, French and others are not used.

Whether the page can close itself is up to the browser. A page may only close
a tab that a script opened, or a tab with a single entry in its history, and
the VPN client opens the sign-in URL as a normal tab. A sign-in that passes
straight through (Entra remembers the account and only redirects) leaves one
entry, so the tab closes, and the browser window with it when it was the only
tab. An interactive sign-in (account picker, password, MFA, *Stay signed
in?*) adds entries, and the browser then ignores the request without an
error; a click on a button would not change that. Half a second after the
attempt the page therefore replaces the countdown with *U kunt dit venster
nu sluiten.* The page also sends the OpenVPN web-auth `CONNECT_SUCCESS`
message, which lets a client that shows the sign-in in its own window close
that window.

## Fail-closed enforcement

Everything SSO does hangs on one directive, `management-client-auth`. OpenVPN
only consults the SSO daemon when its configuration contains it. Without it,
and with the instance's **Authentication** field empty, OpenVPN authenticates a
client on its certificate alone: anyone holding a valid client certificate for
that instance connects without a browser sign-in. The instance form cannot hold
the directive, so the plugin writes it into the instance's configuration
itself, and the form drops it again on every save. Up to 1.4, any save of the
instance followed by **Apply** left the instance open until **Save** was pressed
on the SSO page. Since 1.5 the plugin maintains one rule:

> While SSO is enabled, the protected instance may only run as an OpenVPN
> process whose loaded configuration contained `management-client-auth`. Any
> other process is stopped. Nothing ever removes the directives automatically.

### What you notice

- **You save the instance** under **VPN > OpenVPN > Instances.** The plugin
  writes the directives back within seconds, and again right before every
  **Apply**, boot and CARP event, so the instance is not restarted for it.
  There is nothing to do on the SSO page; *Saved instance directives* returns
  to `present` by itself. You do not need to touch the instance's **Options**
  field either: it cannot show the directives and drops them on every save,
  and the plugin puts them back.
- **The instance ever starts without them**, for example when it is restarted
  from the dashboard a second or two after it was saved. The SSO guard, part
  of the SSO service, checks the configuration every OpenVPN process started
  with, and stops such an instance within about a second, which drops every
  session it had. It then restores the directives and starts the instance
  again. It keeps the instance stopped instead, and the status panel reads
  *instance kept stopped*, when **Repair OpenVPN instance directives** is off,
  when the instance comes back without the directive after a repair, or after
  three repairs in 15 minutes.
- **Repair OpenVPN instance directives** (SSO page, **Advanced** section,
  default on) decides between those two outcomes. With it on, the plugin keeps
  its directives on the instance and repairs an instance found running
  without `management-client-auth`. With it off, the plugin writes nothing into
  the instance and keeps such an instance stopped instead. There is no switch
  that turns the protection itself off while SSO is enabled.
- **You stop only the SSO service** while it is enabled (for example on
  **System > Diagnostics > Services**). Apply, boot and CARP events still
  restore the directives before the instance starts, but a restart of the
  instance from the dashboard, the Services page or **VPN > OpenVPN >
  Connection Status** is then not checked, and the status panel shows *not
  watched* in red.
- **You disable or remove the plugin, or select another instance.** The
  directives stay on the instance the plugin protected. OpenVPN then waits for
  an SSO daemon that never answers and refuses every client after about 60
  seconds. This is deliberate: an instance that quietly falls back to
  certificate-only logins is exactly what the plugin exists to prevent. The
  README's [Maintenance](../README.md#updating-and-removing) section shows how
  to run that instance without SSO.

### The three mechanisms

OpenVPN reads the directive once, when the process starts, and keeps it for the
life of the process; OPNsense never reloads an instance in place. So the check
is per process, and three mechanisms carry it:

1. **Pre-Apply hook.** On **Apply**, at boot and on CARP events, core 26.7
   runs `pluginctl -c crl` right before it regenerates the instance
   configurations. The plugin hooks in there and restores the directives
   first. They are added in the same order as before, so the regenerated file
   is identical and core does not restart the instance for it.
2. **Configuration watcher.** The SSO service notices when `config.xml`
   changes and, when the saved instance lost the directives, writes them back
   within seconds through the same model code. It never restarts anything.
3. **SSO guard.** A thread in the SSO service watches the instance's pidfile.
   For every new OpenVPN process it reads the configuration file and core's
   start record for that start (`instance-<uuid>.stat`, which holds the md5 of
   the configuration on disk right after the start). The process counts as
   enforcing only when that file has a bare `management-client-auth` line and
   matches the recorded md5. Rewrites after the process wrote its pidfile are
   covered by the modification times: one that lands before the start record
   counts against the process, and a later one has to match the md5 as well.
   A process without the directive, or one that cannot be proven within 20
   seconds, is stopped; the guard then restores the directives and starts the
   instance again through core's own `configctl openvpn start`. Core's start
   takes the tunnel interface down before it looks for a running process, so
   the guard first waits (up to 30 seconds) while core is busy with the
   instance, and if something else has started the instance by then, it
   checks that process instead. The guard starts the instance at most once
   per violation and three times in 15 minutes, a start it left to someone
   else included, and otherwise keeps it stopped. A verified process is
   remembered by its pid and pidfile in
   `/var/run/openvpnauthoauth2.guard.json`, so restarting the SSO service does
   not restart a healthy instance, and a repair that such a restart
   interrupted is taken up again.

The guard stops a process with SIGTERM, and sends a second SIGTERM shortly
after. On a UDP instance with **explicit-exit-notify** among its Options,
OpenVPN answers the first one by telling its clients to reconnect elsewhere
and keeps serving for two more seconds; the second makes it exit at once. Only
a process that ignores both for five seconds gets SIGKILL. That skips
OpenVPN's own clean-up, so the guard also removes the stale pidfile and, for
an instance using Data Channel Offload, destroys the `ovpn` interface: the
kernel keeps its own hold on the tunnel's socket and peers until that
interface is gone. Core creates the interface again on the next start. A
process that outlives even SIGKILL is not reported as stopped: the guard keeps
the instance held and sends the signals again every five seconds until it is
gone.

### The status panel in detail

*SSO enforcement (running instance)* is the security row. It reflects the
configuration the running OpenVPN process actually loaded, not what is saved:
*enforced* means that process has `management-client-auth`, so nobody
connects without signing in. Right after the instance starts it reads
*verifying* for a few seconds, and *instance not running* while it is stopped.
A red value means the instance is, or may be, reachable with a certificate
alone, is being kept stopped, or no longer exists. The small line under the
label shows the OpenVPN process ID and, when there is one, the reason.

*Saved instance directives* shows whether the directives are in the saved
configuration, which is what the next start of the instance uses. Right after
the instance is saved it briefly reads *missing, restoring*.
*Silent token renewal* reads *instance's Auth Token Lifetime in use* when that
field on the instance is not empty.

While the SSO daemon runs and the enforcement row reads anything other than
*enforced* or *verifying*, the *SSO daemon* row reads *running, not
consulted*: the daemon runs, but no running instance hands it any logins.
While the instance is not running, or the guard stops, repairs or keeps it
stopped, the daemon is paused and the row reads *not running*.

The *Callback listener* row only proves the service is listening on the
firewall itself; whether your users can reach it from outside is what the
firewall rules in step 5 and the DNS record are for.

### Guard log lines

The guard's log lines start with `SSO guard:`. Search for that text on **VPN >
OpenVPN > Log File** (core files every program whose name contains `openvpn`
there, this plugin included), or in the shell:

```sh
grep -h -e 'SSO guard' -e 'SSO directives' /var/log/openvpn/latest.log /var/log/system/latest.log
```

| Log line contains | Meaning |
|---|---|
| `enforces SSO` | the check after a start passed; normal |
| `restoring the SSO directives in config.xml (config.xml lost them)` | the instance was saved; normal |
| `restored the SSO directives ... before OpenVPN applied its configuration` | the same, caught right before an **Apply**; normal |
| `is running WITHOUT management-client-auth` | the instance was open to any valid certificate and is being stopped |
| `cannot prove ... loaded management-client-auth` | the guard could not verify the instance and restarts it to be sure |
| `stopped OpenVPN instance` | the stop is done, with its duration |
| `starting OpenVPN instance ... with the SSO directives` | repaired and started again |
| `was started again ... checking that process instead` | repaired, but something else (an **Apply**, say) started the instance first; the guard checks that process instead of starting it a second time |
| `resuming the repair of OpenVPN instance` | the SSO service was restarted between stopping the instance and starting it again; the new one finishes the repair |
| `keeping OpenVPN instance ... stopped` | held; the line ends with what to do |
| `could not stop OpenVPN instance` | the process outlived SIGKILL; the guard keeps the instance held and tries again every 5 s |
| `lacks management-client-auth and repair is off` | repair is off; the next start of the instance will be stopped |
| `core no longer runs pluginctl -c crl` | a firmware update changed core; the guard still protects, but an **Apply** right after an instance save may now cost a restart |

The complete list, with log levels, is in
[INVESTIGATION.md](INVESTIGATION.md#guard-log-lines).

### Self-test

To see the protection work, pick a quiet moment: the test stops the instance
and drops its tunnels.

1. On the SSO page, untick **Repair OpenVPN instance directives** and click
   **Save**.
2. Open the instance under **VPN > OpenVPN > Instances**, click **Save**
   without changing anything, then **Apply**. The log shows
   `repair is off`, and once the Apply has started the instance,
   `is running WITHOUT management-client-auth`, `stopped OpenVPN instance` and
   `keeping OpenVPN instance ... stopped`. The status panel reads *instance
   kept stopped*, and clients cannot connect.
3. Start the instance on **System > Diagnostics > Services**. It is stopped
   again at once and stays stopped.
4. Tick **Repair OpenVPN instance directives** again and click **Save**. The
   plugin restores the directives and starts the instance, and the enforcement
   row returns to *enforced*.

### What is left open

Compared with 1.4:

| Path | Up to 1.4 | 1.5 |
|---|---|---|
| Save the instance, then **Apply**; boot; CARP event | open until **Save** on the SSO page | closed: the directives are restored before the configurations are generated, and the instance is not restarted for it |
| Save the instance, then restart only that instance (dashboard, Services page, Connection Status, a cron job, an HA sync) a few seconds later | open | closed: the watcher has already restored the directives |
| The same within a second or two, with repair off, or if core stops writing the directive | open | the guard stops the process within about a second of its start; sessions it admitted die with it, and no auth token is issued to them |
| The SSO service restarts (after a crash, **Save** on the SSO page, or an update) | not applicable | a few seconds unwatched, then a check at startup; a process that was already verified stays trusted, and a repair the restart interrupted is taken up again |
| The SSO service is stopped by hand while enabled | open | Apply, boot and CARP events are still covered; other restarts are not checked, and the status panel shows *not watched* |
| Two core runs overlap (an **Apply** during a per-instance restart, say), and the second rewrites the configuration in the few milliseconds between OpenVPN reading it and writing its pidfile | open | open while that process runs: the start record then matches the rewritten file, which predates the pidfile, so a process that loaded a configuration without the directive passes as enforcing |

Whatever window remains, only someone holding a valid, unrevoked client
certificate issued by the instance's CA can use it: with **Authentication**
empty, core requires a verified client certificate.

An update restarts the SSO service by itself when it is enabled and running:
about five seconds after the package is installed, the new version takes over.
Coming from 1.4, the new guard checks the running instance on its first pass:
if that instance runs without SSO, it is stopped and started again with the
directives, which drops its tunnels once. If *SSO enforcement (running
instance)* still reads *not watched* a minute after an update from 1.4, the
old version, which has no guard, is still running; coming from a later version
the panel cannot tell the two apart. Either way, **Save** on the SSO page or
`configctl openvpnauthoauth2 restart` makes sure the new version runs.

## Background to the setup steps

### Client certificates and the profile

Entra ID is what identifies the person; the client certificate only proves the
device is allowed to reach the tunnel at all. Without `duplicate-cn` under the
instance's **Options**, OpenVPN disconnects a client when another one connects
with the same Common Name, so people who connect at the same time need
certificates of their own: create one per user with a **Description** and
**Common Name** of their own.

A working exported profile looks like this. There is no `proto` line for UDP:
the protocol rides on the `remote` line.

```
dev tun
persist-tun
persist-key
client
resolv-retry infinite
remote vpn.example.com 1194 udp
lport 0
verify-x509-name "..." subject
remote-cert-tls server
<ca>...</ca>
<cert>...</cert>
<key>...</key>
```

Clicking the download icon on the Client Export page also saves the form as
that server's export presets. There is no separate Save button and no
confirmation.

The two red lines in the OpenVPN GUI log that the optional profile edit
removes are cosmetic, and the exporter offers no way to avoid them:

- `DEPRECATED OPTION: --persist-key option ignored`: the exporter writes
  `persist-key` unconditionally, and OpenVPN 2.7 ignores the option entirely.
- `WARNING: this configuration may cache passwords in memory -- use the
  auth-nocache option`: printed whenever the client touches its cached
  credentials, which in this setup are the auth token, never a password.
  Pushed auth tokens are exempt from `auth-nocache`, so silent renewal keeps
  working; after adding it, confirm the first renegotiation (roughly an hour
  in) still passes without a browser.

### Full tunnel

**DNS Servers** matters because OPNsense pushes no DNS server on its own,
redirect gateway or not. Clients then keep their own DNS servers, and any that
are not on their local network (their provider's, or a public one such as
`8.8.8.8`) are now reached through the tunnel as well. Pushing the firewall's
address lets its Unbound resolver answer instead. Unbound accepts queries from
every network by default, so the tunnel network needs no extra entry; only if
you changed **Services > Unbound DNS > Access Lists > Default Action** to
`Deny` or `Refuse` do you need to add an allow entry for `10.10.10.0/24`.

**Redirect gateway** `default` pushes two overriding routes rather than
replacing the client's default route, so nothing is left behind if the client
disconnects uncleanly.

**ipv6 (default)** carries no IPv6 through the tunnel without a **Server
(IPv6)** network: OpenVPN 2.7 clients ignore it, so IPv6 keeps using the
client's own connection, and OpenVPN 2.6 clients send IPv6 into a tunnel that
cannot carry it. Select it only when the instance also has a **Server (IPv6)**
tunnel network that the firewall can route to the internet, with IPv6 firewall
rules to match. If you would rather block IPv6 on clients while they are
connected, so none of it bypasses the tunnel, OpenVPN documents a way: fill in
**Server (IPv6)** with a private range such as `fd15:53b6:dead::/64`, select
**ipv6 (default)** next to **default**, and select `block-ipv6` under
**Options**. The firewall then answers the clients' IPv6 packets with *no route
to host*, so no IPv6 leaves around the tunnel.

OPNsense's automatic Source NAT (outbound NAT) only translates the networks of
LAN-type interfaces (assigned, with an address and no gateway), the IPsec
mobile pool and legacy OpenVPN servers. It does not include the tunnel network
of an OpenVPN *instance*, so redirected traffic would leave WAN with its
private `10.10.10.x` source address and the replies never come back. OPNsense's
own road-warrior guide for instances says the same. Hybrid mode keeps the
automatic rules for your LAN and adds your own; in **Automatic** mode the rule
list is read-only and rules of your own are not loaded.

Routing and filtering are separate decisions: **Local Network** on the
instance tells clients the tunnel is the way to reach a network, and the
OpenVPN firewall rule is what actually permits the traffic once it arrives.
Tighten that rule's **Protocol**, **Source** and **Destination** when not every
client should reach everything; identity-based per-user rules are not possible
on this hop, since the firewall sees only tunnel IP addresses.

### The package repository

The SurfHost repository carries this plugin and a mirrored copy of the
`openvpn-auth-oauth2` daemon it depends on, nothing else. It registers at a
lower pkg priority than OPNsense's own repository (priority 11), so if both
ever offered the same package name, OPNsense's copy is the one that gets
installed. Priority is a preference, not a sandbox: it does not apply to
packages only this repository provides, and `pkg install -r SurfHost` bypasses
it. Adding the repository means trusting SurfHost to the same degree as any
other package source on the firewall.

Installing with `pkg install` puts the files in place but never registers the
plugin in OPNsense's configuration, so the Plugins tab marks it
**(misconfigured)**. It runs fine, but OPNsense rebuilds the plugin set from
that registration list, so a plugin missing from it is *not* reinstalled after
a configuration restore or a firmware sync. Removing it with a bare
`pkg delete` leaves the registration behind in the same way.

### The ACME certificate

DNS-01 validation never requires an inbound connection to the firewall, so
port 80 stays closed. It proves you control the zone by publishing a TXT
record at `_acme-challenge.vpn.example.com`, and says nothing about where
`vpn.example.com` points: the browser callback still needs the public **A**
record. A Cloudflare-proxied record lets the certificate issue perfectly and
only breaks the callback, which is why the README asks for **DNS only**.

Leave **DNS Sleep Time** at `0`: the plugin then polls *public* DNS every 10
seconds until the record appears. Any non-zero value switches it to a fixed
wait *and* to querying your local resolver, which on a firewall running
Unbound can answer differently from the internet and fail confusingly.
Registering the ACME account before the first issuance surfaces a bad e-mail
address or CA choice before you spend a rate-limited issuance attempt.
