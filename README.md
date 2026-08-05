# OpenVPN single sign-on with Microsoft Entra ID for OPNsense

Sign in to your OpenVPN connection with your Microsoft work account, in a real
browser window, with Conditional Access and MFA applied. No passwords in the
VPN client, no RADIUS server, no Azure infrastructure.

This is an OPNsense plugin (`os-openvpn-auth-oauth2`) that packages
[openvpn-auth-oauth2](https://github.com/jkroepke/openvpn-auth-oauth2) and wires
it into OPNsense's **OpenVPN Instances**.

**What a user sees:** they connect with their normal OpenVPN client, a browser
tab opens at the Microsoft sign-in page, they authenticate (MFA, Conditional
Access, whatever your tenant requires), and the tunnel comes up. Reconnects are
silent while the auth token is valid.

---

## Contents

1. [What you need](#what-you-need)
2. [Step 1: Register the application in Entra ID](#step-1-register-the-application-in-entra-id)
3. [Step 2: Prepare the OpenVPN server](#step-2-prepare-the-openvpn-server)
4. [Step 3: Install the plugin](#step-3-install-the-plugin)
5. [Step 4: Configure the plugin](#step-4-configure-the-plugin)
6. [Step 5: Connect a client](#step-5-connect-a-client)
7. [Troubleshooting](#troubleshooting)
8. [How it works](#how-it-works)
9. [Maintainer notes](#maintainer-notes)

---

## What you need

| | |
|---|---|
| **Firewall** | OPNsense 26.7 or newer |
| **Entra ID** | A tenant where you can create an app registration (Application Administrator or higher) |
| **DNS** | A hostname that resolves to your firewall's WAN address, e.g. `vpn.example.com` |
| **Certificate** | A server certificate for that hostname, trusted by your users' browsers (a Let's Encrypt certificate from the **os-acme-client** plugin works well) |
| **Client** | OpenVPN GUI 2.6+ (Windows), Tunnelblick 4.0.0b10+ (macOS), Viscosity, or OpenVPN3 3.9+ |

Two ports must be reachable from the internet: the OpenVPN port itself
(UDP 1194 by default) and the browser callback port (TCP 9443 by default).

> **Note on clients:** NetworkManager on Linux does not support browser
> authentication and cannot be used. OpenVPN Connect v3 works only partially.
> Use one of the clients listed above.

---

## Step 1: Register the application in Entra ID

You are creating an application that represents your VPN, so Entra ID knows who
is asking when a user signs in.

### 1.1 Create the app registration

1. Go to the [Microsoft Entra admin center](https://entra.microsoft.com) and
   sign in.
2. Navigate to **Identity > Applications > App registrations**.
3. Click **New registration**.
4. Fill in:
   - **Name**: something recognisable, e.g. `OPNsense OpenVPN SSO`
   - **Supported account types**: *Accounts in this organizational directory
     only (single tenant)*
   - **Redirect URI**: select platform **Web** and enter:
     ```
     https://vpn.example.com:9443/oauth2/callback
     ```
     Replace `vpn.example.com` with your own hostname. Keep `:9443` and
     `/oauth2/callback` exactly as shown unless you change the port later.
5. Click **Register**.

On the **Overview** page that opens, copy these two values, you will need them:

- **Application (client) ID**
- **Directory (tenant) ID**

### 1.2 Create a client secret

1. In your new app registration, go to **Certificates & secrets**.
2. Under **Client secrets**, click **New client secret**.
3. Give it a description and an expiry (24 months is the maximum).
4. Click **Add**, then **immediately copy the `Value` column**. It is shown only
   once, and the `Secret ID` is not the value you need.

> ⚠️ **Put the expiry date in your calendar.** When the secret expires, VPN
> logins stop working, with no warning beforehand.

### 1.3 Check API permissions

1. Go to **API permissions**.
2. You need the delegated Microsoft Graph permissions **openid**, **profile**
   and **offline_access**. `User.Read` is usually present by default and already
   implies `openid` and `profile`; add anything missing with
   **Add a permission > Microsoft Graph > Delegated permissions**.
3. `offline_access` is what allows silent reconnects, so do not skip it.
4. Click **Grant admin consent for &lt;tenant&gt;** so users are not prompted
   individually.

### 1.4 Decide who may connect (recommended)

The simplest and most robust method is to let Entra ID do the filtering:

1. Go to **Identity > Applications > Enterprise applications** and open the
   application you just registered.
2. **Properties > Assignment required?** > **Yes** > **Save**.
3. **Users and groups > Add user/group** and assign the people or groups who may
   use the VPN.

Anyone not assigned is refused by Microsoft during sign-in, before your firewall
is ever involved.

*Alternative:* if you prefer to filter on the firewall, configure a groups claim
(**Token configuration > Add groups claim > Security groups**, emitted as group
**object IDs**) and list those object IDs in the plugin's *Allowed groups* field
later. Note that Entra omits the claim entirely for users in more than 200
groups, which then denies them.

### 1.5 Optional hardening

Under **Security > Conditional Access**, create a policy scoped to this
application to require MFA, a compliant device, or specific named locations.
This is the main reason to use SSO rather than passwords, so it is worth doing.

### What to write down

| Value | Where you found it | Example |
|---|---|---|
| Directory (tenant) ID | App registration > Overview | `2c9f...-...-...-...-...b81e` |
| Application (client) ID | App registration > Overview | `7a1b...-...-...-...-...4f3d` |
| Client secret value | Certificates & secrets | `abc8Q~...` |
| Public base URL | Chosen by you | `https://vpn.example.com:9443` |

---

## Step 2: Prepare the OpenVPN server

**Already have a working OpenVPN server instance?** Skip to
[2.4](#24-checklist-for-an-existing-instance) and just check three settings.

### 2.1 Certificates

Under **System > Trust** you need:

1. **Authorities**: an internal Certificate Authority (create one if you have
   none: *Add > Method: Create an internal Certificate Authority*).
2. **Certificates**: a server certificate issued by that CA, with **Type:
   Server Certificate**.

These secure the VPN tunnel itself. The certificate for the browser callback is
a separate one, see [4.1](#41-certificate-for-the-callback-listener).

### 2.2 Create the instance

Go to **VPN > OpenVPN > Instances** and click **+**. The settings that matter:

| Field | Value |
|---|---|
| **Role** | `Server` |
| **Description** | e.g. `SSO VPN` |
| **Protocol** / **Port number** | `UDP` / `1194` |
| **Type** | `tun` |
| **Certificate** | the server certificate from 2.1 |
| **Verify Client Certificate** | `Required` |
| **Server (IPv4)** | a free subnet for VPN clients, e.g. `10.10.0.0/24` |
| **Authentication** | **leave empty** (see the warning below) |
| **Auth Token Lifetime** | `28800` (8 hours) |
| **Renegotiate time** | leave at the default `3600` |
| **Local Network** | the networks clients should reach, e.g. `192.168.1.0/24` |

Click **Save**, then enable the instance.

There is no *Certificate Authority* field to fill in unless you enable
**advanced mode**; the CA is taken from the certificate you selected. Only set
it if your CA differs from the one that issued that certificate.

> **Do not set *Renegotiate time* to 0.** OPNsense rejects that combination with
> *"A token lifetime requires a non zero Renegotiate time"*. Renegotiation is
> harmless here: the client presents its auth token instead of returning to the
> browser, which is exactly what **Auth Token Lifetime** is for.

> ⚠️ **Leave *Authentication* empty.** It sits under the *Authentication*
> section and normally points at a local or LDAP user database. If you set it,
> users must pass *both* that backend *and* Entra ID, which is not what you
> want here. Identity comes from Entra ID.

**Auth Token Lifetime** and **Renegotiate time** are what keep users from being
sent back to the browser mid-session: the client receives a token on first login
and reuses it silently until it expires.

You do **not** need to touch the **Options** field. The plugin adds the required
`management-client-auth` directive there itself when you save its settings.

### 2.3 Firewall rule for the VPN port

**Firewall > Rules > WAN**, add a rule:

| Field | Value |
|---|---|
| Action | Pass |
| Protocol | UDP |
| Destination | WAN address |
| Destination port | 1194 |

Also check **Firewall > Rules > OpenVPN** allows the traffic you want VPN
clients to reach.

### 2.4 Checklist for an existing instance

If you already run OpenVPN, open the instance under **VPN > OpenVPN >
Instances** and confirm:

- [ ] **Authentication** is empty
- [ ] **Auth Token Lifetime** is set (e.g. `28800`) and **Renegotiate time** is
      non-zero (the default `3600` is fine)
- [ ] **Verify Client Certificate** is `Required`, and your users have client
      certificates

Nothing else changes; existing clients keep working until you switch them over.

---

## Step 3: Install the plugin

### 3.1 Add the SurfHost repository

The plugin is not in the official OPNsense repository, so add ours once. SSH
into the firewall (or use **Interfaces > Diagnostics > Command**) as `root`:

```sh
fetch -o /usr/local/etc/pkg/repos/surfhost.conf https://surfhost.github.io/opnsense-plugin-entra-sso/surfhost.conf
pkg update
```

The repository serves only SurfHost plugins and sits at a lower priority than
the official one, so it cannot replace OPNsense packages.

### 3.2 Install the plugin

In the GUI, go to **System > Firmware > Plugins**, search for
`os-openvpn-auth-oauth2` and click **+** to install.

Or from the shell:

```sh
pkg install os-openvpn-auth-oauth2
```

After installation a new menu entry appears: **VPN > OpenVPN > SSO (OAuth2 /
Entra ID)**. If you do not see it, force a reload with
`service configd restart && service php_fpm restart`.

### 3.3 Updating and removing

Updates arrive through the normal **System > Firmware > Updates** flow once the
repository is added. To remove everything:

```sh
pkg delete os-openvpn-auth-oauth2
rm /usr/local/etc/pkg/repos/surfhost.conf
pkg update
```

---

## Step 4: Configure the plugin

### 4.1 Certificate for the callback listener

The browser connects to your firewall at `https://vpn.example.com:9443`, so that
listener needs a certificate your users' browsers trust. A self-signed
certificate produces a warning page and some VPN clients refuse it outright.

The easiest route is the **os-acme-client** plugin: install it, create a
Let's Encrypt certificate for `vpn.example.com`, and it lands in
**System > Trust > Certificates** ready to select. Alternatively import a
certificate you already own.

### 4.2 Fill in the settings

Go to **VPN > OpenVPN > SSO (OAuth2 / Entra ID)**:

| Field | Value |
|---|---|
| **Enable** | ticked |
| **OpenVPN instance** | the instance from step 2 |
| **Tenant ID** | Directory (tenant) ID from step 1 |
| **Client ID** | Application (client) ID from step 1 |
| **Client secret** | the secret *value* from step 1 |
| **Allowed groups** | leave empty if you used *Assignment required* in 1.4 |
| **Public base URL** | `https://vpn.example.com:9443`, must match the redirect URI in Entra exactly |
| **Listen port** | `9443` |
| **Encryption secret** | 16, 24 or 32 random letters and digits |
| **Enable TLS** | ticked |
| **Certificate** | the certificate from 4.1 |

Generate the encryption secret with:

```sh
openssl rand -hex 16
```

It encrypts browser session cookies and stored refresh tokens. Changing it later
forces everyone to sign in again.

Click **Save**. The plugin now:

- writes the daemon configuration,
- adds `management-client-auth` to your OpenVPN instance and **restarts that
  instance** (active tunnels drop once, here),
- starts the SSO service.

### 4.3 Firewall rule for the callback port

**Firewall > Rules > WAN**, add a second rule:

| Field | Value |
|---|---|
| Action | Pass |
| Protocol | TCP |
| Destination | WAN address |
| Destination port | 9443 |

Restrict the source to the networks your users browse from if you can. The
listener only serves OAuth2 endpoints, but there is no reason to expose it more
widely than necessary.

### 4.4 Check the status panel

The top of the SSO settings page shows five rows. For a healthy setup:

| Row | Expected |
|---|---|
| Supervisor | running |
| SSO daemon | running |
| Management socket swap | active |
| Callback listener | reachable |
| OpenVPN 'management-client-auth' | present |

If anything is off, see [Troubleshooting](#troubleshooting).

---

## Step 5: Connect a client

### 5.1 Create a client certificate

Each user needs their own client certificate, because the instance is set to
**Verify Client Certificate: Required** and the certificate is what
authenticates the VPN client itself. Entra ID then decides *who the person is*
on top of that.

Go to **System > Trust > Certificates**, click **Add**, and choose:

| Field | Value |
|---|---|
| **Method** | `Create an internal certificate` |
| **Certificate authority** | the CA that issued your server certificate |
| **Type** | `Client Certificate` |
| **Common Name** | the user, e.g. `hans` |

### 5.2 Export the profile

Go to **VPN > OpenVPN > Client Export** and select your instance. The list
underneath shows one row per client certificate. Download the `.ovpn` from the
row for the certificate you just created, using export type **File Only**.

Open the file in a text editor and confirm it contains a `<cert>` block and a
`<key>` block. Those are the client's authentication method, and without them
OpenVPN refuses to start with:

```
Options error: No client-side authentication method is specified.
```

The profile does **not** need `auth-user-pass`: the certificate satisfies
OpenVPN's client-authentication requirement, and the identity check happens in
the browser. If your client disconnects instead of waiting for the browser, add
`auth-retry interact`.

### 5.3 First login

1. Import the profile into OpenVPN GUI, Tunnelblick or Viscosity.
2. Connect.
3. Your default browser opens at the Microsoft sign-in page.
4. Sign in and complete MFA if your tenant requires it.
5. The browser shows a success page, and the tunnel comes up.

Reconnecting within the **Auth Token Lifetime** you configured happens silently,
without a browser prompt.

You can confirm the session under **VPN > OpenVPN > Connection Status**, which
keeps working normally while SSO is active.

---

## Troubleshooting

**Start here:** the status panel on the SSO page, plus the log at
**System > Log Files > General**, filtered on `openvpn-auth-oauth2`. From the
shell:

```sh
configctl openvpnauthoauth2 details
```

| Symptom | Cause and fix |
|---|---|
| `pkg update` gives a 404 | Your ABI directory does not exist yet in the repository. Compare `pkg config abi` (OPNsense 26.7 reports `FreeBSD:15:amd64`) with the directories published in the repository. |
| Plugin menu entry missing after install | `service configd restart && service php_fpm restart` |
| Status: **management-client-auth missing** | Someone re-saved the OpenVPN instance in the GUI, which silently drops the directive. Press **Save** on the SSO page to restore it. |
| Status: **daemon not running** | Usually a bad tenant/client ID or an unreachable Entra endpoint. Check the log for the actual error. |
| Status: **callback listener unreachable** | The daemon failed to bind, often a certificate problem or a port already in use. Check the log and `sockstat -l \| grep 9443`. **Do not use port 9000**: OPNsense's php-fpm listens on `127.0.0.1:9000`, so binding the wildcard address there fails. |
| `Options error: No client-side authentication method is specified` | The exported profile has no `<cert>`/`<key>` block. Export it from the row of a **Client Certificate** in Client Export, not from a profile without one. |
| Browser never opens on connect | The client does not support browser authentication, or the profile disconnects too early (try adding `auth-retry interact`). |
| Browser opens but cannot load the page | DNS for your base URL does not point at the firewall, or the WAN rule for TCP 9443 is missing. |
| Certificate warning in the browser | The listener certificate is self-signed or does not match the hostname in the base URL. |
| `AADSTS7000215` (invalid client secret) | Wrong secret, or the *Secret ID* was copied instead of the *Value*. |
| `AADSTS50011` (redirect URI mismatch) | The redirect URI in Entra must be exactly your base URL plus `/oauth2/callback`. |
| Login succeeds but VPN is refused | The user is not assigned to the enterprise application, or is not in a group listed under *Allowed groups*. |
| Everyone is suddenly refused | The Entra client secret expired. Create a new one and paste the value into the plugin. |

---

## How it works

Users connect with a certificate-based profile. The OpenVPN server defers
authentication to the `openvpn-auth-oauth2` daemon over its management
interface; the daemon sends the client a `WEB_AUTH::` URL, the user signs in to
Entra ID in a browser, and the daemon approves the session with an auth token
that makes later reconnects silent.

OPNsense's OpenVPN instances already use the management socket for the GUI's
Connection Status page, and OpenVPN allows only one management client. The
plugin resolves that with a socket swap plus the daemon's pass-through proxy, so
both the SSO daemon and the GUI keep working. The full design, the alternatives
that were rejected, and the known limitations are in
[docs/INVESTIGATION.md](docs/INVESTIGATION.md).

---

## Maintainer notes

### Development loop

Test changes without building a package:

```sh
rsync -av os-openvpn-auth-oauth2/src/ root@fw:/usr/local/
ssh root@fw 'rm -rf /tmp/opnsense_mvc_cache*; service configd restart; service php_fpm restart'
```

Useful checks on the box:

```sh
configctl template reload OPNsense/OpenVPNAuthOAuth2
configctl openvpnauthoauth2 start
configctl openvpnauthoauth2 details
grep management-client-auth /var/etc/openvpn/instance-*.conf
```

> **Shell note:** root's login shell on OPNsense is tcsh, which does not
> understand `$(...)` command substitution or `VAR=value command` prefixes.
> Run `sh` once before pasting the commands in this section.

### The daemon dependency

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

### Building and publishing a release

Run on an OPNsense box of the **same major release** you are publishing for
(the build host's ABI is stamped into the package), from a checkout of this
repository:

```sh
DAEMON_PKG=/tmp/openvpn-auth-oauth2.pkg PUBLISH=1 ./tools/publish-repo.sh
```

That fetches the opnsense/plugins tree if needed, builds the plugin, copies the
daemon package alongside it, generates the `pkg` metadata for the current ABI,
and pushes the result to the `gh-pages` branch that GitHub Pages serves. Without
`PUBLISH=1` it stages the files and prints the manual publish commands.

Bump `PLUGIN_VERSION` in
[`os-openvpn-auth-oauth2/Makefile`](os-openvpn-auth-oauth2/Makefile) before
building a new release, and tag the commit.

> The published tree is keyed by ABI (`FreeBSD:15:amd64` for OPNsense 26.7), and
> `surfhost.conf` uses `${ABI}`, so firewalls pick the right directory
> automatically. An OPNsense release that moves to a new FreeBSD major needs a
> fresh build published under the new ABI. Never check out the `gh-pages` branch
> on Windows: those directory names contain colons, which NTFS forbids.

---

## License

MIT, see [LICENSE](LICENSE). The plugin packages
[openvpn-auth-oauth2](https://github.com/jkroepke/openvpn-auth-oauth2) by Jan-Otto
Kröpke, also MIT licensed.
