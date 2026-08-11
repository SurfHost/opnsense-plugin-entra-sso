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
2. [Quick checklist](#quick-checklist)
3. [Step 1: Register the application in Entra ID](#step-1-register-the-application-in-entra-id)
4. [Step 2: Prepare the OpenVPN server](#step-2-prepare-the-openvpn-server)
5. [Step 3: Install the plugin](#step-3-install-the-plugin)
6. [Step 4: Configure the plugin](#step-4-configure-the-plugin)
7. [Step 5: Firewall rules](#step-5-firewall-rules)
8. [Step 6: Connect a client](#step-6-connect-a-client)
9. [Troubleshooting](#troubleshooting)
10. [How it works](#how-it-works)
11. [Maintainer notes](#maintainer-notes)

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

The plugin protects **one** OpenVPN instance per firewall in this version.
Other instances keep working normally, they just do not get SSO.

---

## Quick checklist

Complete every item and you have a working SSO setup. The numbered steps
below cover each item in detail.

**Entra ID**

- [ ] Create the app registration (single tenant)
- [ ] Add Web redirect URI `https://vpn.example.com:9443/oauth2/callback`
- [ ] Create a client secret and copy its value
- [ ] Permissions `openid`, `profile`, `offline_access`, grant admin consent
- [ ] Enterprise app: *Assignment required* = Yes, assign your users
- [ ] Note the tenant ID and client ID

**DNS and certificate**

- [ ] DNS A record for the VPN hostname, pointing at the firewall WAN
- [ ] Let's Encrypt certificate for that hostname (os-acme-client)

**OpenVPN instance**

- [ ] Create CA, server certificate and client certificate, each with a Common Name
- [ ] Create the server instance: UDP 1194, tunnel network, local network
- [ ] Authentication empty, Auth Token Lifetime empty, Renegotiate time empty
- [ ] Keep alive interval `10`, timeout `60` (advanced mode)

**Plugin**

- [ ] Enable SSH in OPNsense
- [ ] Add the SurfHost repository, run `pkg update`
- [ ] Install `os-openvpn-auth-oauth2` from the Plugins page
- [ ] Fill in the SSO page: instance, tenant ID, client ID, client secret, base URL, encryption secret, TLS certificate
- [ ] Save, then check every status row is green

**Firewall**

- [ ] WAN rules: pass UDP 1194 and TCP 9443
- [ ] OpenVPN interface rule: pass tunnel network to LAN

**Client**

- [ ] Export the profile: File Only, real hostname, the client certificate row
- [ ] Optional: delete `persist-key`, add `auth-nocache`
- [ ] Import, connect, sign in once in the browser

---

## Step 1: Register the application in Entra ID

You are creating an application that represents your VPN, so Entra ID knows who
is asking when a user signs in.

### 1.1 Create the app registration

1. Go to the [Microsoft Entra admin center](https://entra.microsoft.com) and
   sign in.
2. Navigate to **Entra ID > App registrations**.
3. Click **New registration**.
4. Fill in:
   - **Name**: something recognisable, e.g. `OPNsense OpenVPN SSO`
   - **Supported account types**: *Single tenant only - &lt;your tenant&gt;*
5. Click **Register**.
6. Add the redirect URI: in the new app registration go to
   **Manage > Authentication**. On the **Redirect URI configuration** tab, click
   **Add Redirect URI**, choose the **Web** platform tile, enter:
   ```
   https://vpn.example.com:9443/oauth2/callback
   ```
   and click **Configure**. Replace `vpn.example.com` with your own hostname.
   Keep `:9443` and `/oauth2/callback` exactly as shown unless you change the
   port later.

On the app registration's **Overview** page, copy these two values, you will
need them:

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

1. Go to **Entra ID > Enterprise apps > All applications** and open the
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

Under **Entra ID > Conditional Access > Policies**, create a policy scoped to
this application to require MFA, a compliant device, or specific named locations.
This is the main reason to use SSO rather than passwords, so it is worth doing.

### Reusing an existing app registration

Moving to a different firewall, or rebuilding one? The app registration itself
needs no changes, but three things must still line up:

1. **The redirect URI must match the new firewall's public base URL exactly**,
   including scheme, hostname and port. If the hostname or port changes, add
   the new URI on the **Authentication** page (**Redirect URI configuration**
   tab). An app registration can
   hold several, so you can keep the old one during a migration.
2. **DNS for that hostname must point at the new firewall.**
3. **The listener certificate must exist on the new firewall**, since it lives
   in that box's trust store rather than in Entra. Re-issue it with
   **os-acme-client**, or export and import it.

The tenant ID, client ID and client secret carry over unchanged.

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
[2.3](#23-checklist-for-an-existing-instance) and just check three settings.

### 2.1 Certificates

Create three things under **System > Trust**, in this order.

**The certificate authority.** Go to **Authorities** and click **Add**:

| Field | Value |
|---|---|
| **Method** | `Create an internal Certificate Authority` |
| **Description** | `OpenVPN` |
| **Common Name** | `OpenVPN` |

**The server certificate.** Go to **Certificates** and click **Add**:

| Field | Value |
|---|---|
| **Method** | `Create an internal Certificate` (the default) |
| **Description** | `OpenVPN server` |
| **Type** | `Server Certificate` |
| **Issuer** | `OpenVPN` |
| **Common Name** | `vpn.example.com` |

**A client certificate.** Still under **Certificates**, click **Add** again:

| Field | Value |
|---|---|
| **Method** | `Create an internal Certificate` (the default) |
| **Description** | `OpenVPN client` |
| **Type** | `Client Certificate` |
| **Issuer** | `OpenVPN` |
| **Common Name** | `vpn` |

The **Common Name** is not optional on either certificate. A certificate created
without one gets a subject like `/C=NL`, and the client export in step 6 then
fails with *"Client certificate not found"* because there is no name to write
into the profile. After creating them, check the **Name** column in the
certificate list: it must read `/CN=vpn`, not just `/C=NL`.

If you would rather give each person their own certificate, repeat the third
step per user with a **Description** and **Common Name** of their own. Entra ID
is what identifies the person either way; the client certificate only proves the
device is allowed to reach the tunnel at all.

### 2.2 Create the instance

Go to **VPN > OpenVPN > Instances** and click **+**. The *Edit Instance* dialog
is grouped into sections; these are the fields that matter, in the order you
meet them.

**General Settings**

| Field | Value |
|---|---|
| **Role** | `Server` |
| **Description** | e.g. `OpenVPN SSO` |
| **Enabled** | ticked |
| **Protocol** | `UDP` |
| **Port number** | `1194` |
| **Type** | `TUN` |
| **Server (IPv4)** | a free subnet for VPN clients, e.g. `10.10.10.0/24` |

**Trust**

| Field | Value |
|---|---|
| **Certificate** | `OpenVPN server` from 2.1 |
| **Verify Client Certificate** | `require` (the default) |

**Authentication**

| Field | Value |
|---|---|
| **Authentication** | **leave empty** (see the warning below) |
| **Auth Token Lifetime** | **leave empty** (the plugin manages this, see below) |

**Routing**

| Field | Value |
|---|---|
| **Local Network** | the networks clients should reach, e.g. `192.168.1.0/24` |

**Keep alive**

These two fields only appear after you enable **advanced mode**, the toggle at
the top of the dialog:

| Field | Value |
|---|---|
| **Keep alive interval** | `10` |
| **Keep alive timeout** | `60` |

Do not skip them. Without keepalive the server sends nothing on an idle
tunnel, the NAT mapping between client and firewall expires, and the tunnel
silently dies and reconnects about every two idle minutes. Both fields must be
set together, and the timeout must be at least twice the interval.

Click **Save**. The instance is running immediately, because **Enabled** is part
of the form; there is no separate step to switch it on afterwards.

Leave **Renegotiate time** empty as well. OpenVPN renegotiates the session
keys roughly hourly, and the plugin injects an
`auth-gen-token <lifetime> external-auth` directive into the instance (the
lifetime is configurable under the SSO page's Advanced settings, default 24
hours) so the SSO daemon renews the auth token silently at each
renegotiation, without a browser. This only works while the instance's
**Auth Token Lifetime** field stays empty: a value there emits a second
`auth-gen-token` line, so the plugin skips its injection to keep the
instance bootable, and every renegotiation then falls back to a reconnect
with a browser round-trip.

There is no *Certificate Authority* field to fill in unless you enable
**advanced mode**; the CA is taken from the certificate you selected. Only set
it if your CA differs from the one that issued that certificate.

> ⚠️ **Leave *Authentication* empty.** It sits under the *Authentication*
> section and normally points at a local or LDAP user database. If you set it,
> users must pass *both* that backend *and* Entra ID, which is not what you
> want here. Identity comes from Entra ID.

You do **not** need to touch the **Options** field under *Miscellaneous*. When
you save the plugin's own settings in step 4, the plugin adds the required
`management-client-auth` directive there itself. Saving this instance form never
adds it, and re-saving the instance later silently removes it again (see
[Troubleshooting](#troubleshooting)).

#### Sending all client traffic through the VPN

By default only the **Local Network** routes are pushed, so clients reach your
LAN through the tunnel and everything else keeps going out over their own
internet connection. To send *all* their traffic through the firewall instead,
open **Miscellaneous > Redirect gateway** and select **default**.

That option is OpenVPN's `def1` flag, which is why the dropdown does not say
`def1`. It works by pushing two overriding routes rather than replacing the
client's default route, so nothing is left behind if the client disconnects
uncleanly.

The field accepts more than one value. If your clients have IPv6, also select
**ipv6 (default)**, otherwise IPv6 traffic keeps bypassing the tunnel while IPv4
goes through it. Leaving the field empty is a normal split tunnel.

### 2.3 Checklist for an existing instance

If you already run OpenVPN, open the instance under **VPN > OpenVPN >
Instances** and confirm:

- [ ] **Authentication** is empty
- [ ] **Auth Token Lifetime** is **empty** (the plugin injects its own token
      directive with `external-auth`; a value here blocks that and brings the
      browser back at every renegotiation), and **Renegotiate time** is empty
      or non-zero
- [ ] **Verify Client Certificate** is `require`, and your users have client
      certificates
- [ ] **Keep alive interval** and **Keep alive timeout** are set (e.g. `10` and
      `60`, visible in advanced mode), so an idle tunnel does not die to NAT
      timeouts

Nothing else changes yet; existing clients keep working. Be aware the cutover
is instance-wide: once you save the plugin settings in step 4, every client of
this instance goes through the browser sign-in on its next connect. Their
existing profiles keep working, but clients without browser support, such as
NetworkManager, will no longer connect.

---

## Step 3: Install the plugin

### 3.1 Add the SurfHost repository

The plugin is not in the official OPNsense repository, so add ours once. SSH
into the firewall as `root`, or use option **8) Shell** on the console
(OPNsense has no GUI page for running shell commands).

SSH is disabled on a fresh OPNsense install. Enable it under
**System > Settings > Administration**, section **Secure Shell**:

- **Enable Secure Shell**: ticked
- **Permit root user login**: ticked
- **Permit password login**: ticked

Click **Save** at the bottom of the page. The default firewall rules only
accept SSH from the LAN side, which is all this needs. For permanent SSH
access, prefer key-based logins over passwords; if you only enabled SSH for
this installation, untick the three options again afterwards.

Then, on the shell:

```sh
fetch -o /usr/local/etc/pkg/repos/surfhost.conf https://surfhost.github.io/opnsense-plugin-entra-sso/surfhost.conf
pkg update
```

The repository carries this plugin and a mirrored copy of the
`openvpn-auth-oauth2` daemon it depends on, nothing else. It registers at a
lower pkg priority than OPNsense's own repository (priority 11), so if both
ever offered the same package name, OPNsense's copy is the one that gets
installed.

> Priority is a preference, not a sandbox. It does not apply to packages only
> this repository provides, and `pkg install -r surfhost` bypasses it. Adding
> the repository means trusting SurfHost to the same degree as any other
> package source on the firewall.

### 3.2 Install the plugin

Install from the GUI, not from the shell. Go to **System > Firmware > Plugins**
and first click **Click to view the community plugins.**, because plugins from a
third-party repository are hidden until you do. Type `os-openvpn-auth-oauth2`
in the **Name** box, click the **+** on its row, and confirm the **Third party
software** dialog with **Install**.

The row then reads *os-openvpn-auth-oauth2 (installed)*. **Tier** shows `4` and
**Repository** shows `surfhost`; that is correct and permanent, because OPNsense
reserves tiers 1 to 3 for its own and its partners' repositories.

> **Do not install it with `pkg install`.** That puts the files in place but
> never registers the plugin in OPNsense's configuration, so the Plugins tab
> marks it **(misconfigured)**. It runs fine, but OPNsense rebuilds the plugin
> set from that registration list, so a plugin missing from it is *not*
> reinstalled after a configuration restore or a firmware sync. If you already
> did it, repair it once under **System > Firmware > Status** with **Resolve
> plugin conflicts > Reset all local conflicts**, or from the shell with
> `configctl firmware resync`. The row should then read *(installed)*.

After installation a new menu entry appears: **VPN > OpenVPN > SSO (OAuth2 /
Entra ID)**. If you do not see it, the menu cache is stale; clear it and reload:

```bash
rm -f /var/lib/php/tmp/opnsense_menu_cache.xml /var/lib/php/tmp/opnsense_acl_cache.json && service configd restart
```

### 3.3 Updating and removing

Updates arrive through the normal **System > Firmware > Updates** flow once the
repository is added.

Remove the plugin the same way it was installed: on **System > Firmware >
Plugins**, use the remove action on its row. That also removes it from
OPNsense's plugin registration, which a bare `pkg delete` would leave behind,
the mirror image of the warning above. Then drop the daemon package and the
repository:

```sh
pkg delete openvpn-auth-oauth2
rm /usr/local/etc/pkg/repos/surfhost.conf
pkg update
```

---

## Step 4: Configure the plugin

### 4.1 Certificate for the callback listener

The browser connects to your firewall at `https://vpn.example.com:9443`, so that
listener needs a certificate your users' browsers trust. A self-signed
certificate produces a warning page and some VPN clients refuse it outright.

This is a *different* certificate from the one on the OpenVPN instance. The
instance certificate comes from your internal CA, because OpenVPN validates
client certificates against the CA that issued it. This one is publicly trusted
and only ever faces a browser. Do not swap them.

The route below uses **os-acme-client** with a Let's Encrypt certificate
validated over DNS, through Cloudflare. DNS-01 is worth the extra setup here
because it never requires an inbound connection to the firewall, so you do not
have to open port 80. If you already own a certificate for the hostname, import
it under **System > Trust > Certificates** instead and skip to 4.2.

#### Create a Cloudflare API token

In the Cloudflare dashboard, go to **My Profile > API Tokens** and select
**Create Token**, then **Create Custom Token**. Give it exactly two permissions:

| Group | Resource | Level |
|---|---|---|
| Zone | DNS | Edit |
| Zone | Zone | Read |

Under **Zone Resources** choose **Include > Specific zone >** your domain, so
the token cannot touch anything else. Finish with **Continue to summary** and
**Create Token**, then copy the token, which is shown only once.

You also need two identifiers: open your zone's **Overview** page and copy the
**Zone ID** and **Account ID** from the **API** panel on the right.

#### Install and configure the plugin

1. **System > Firmware > Plugins**, install `os-acme-client`. It is in
   OPNsense's own repository, so no extra repository is needed.
2. **Services > ACME Client > Settings**, tick **Enable Plugin** and click
   **Apply**.
3. **Services > ACME Client > Accounts**, click **+**:

   | Field | Value |
   |---|---|
   | **Name** | e.g. `letsencrypt` |
   | **E-Mail Address** | your address, used for expiry warnings |
   | **ACME CA** | `Let's Encrypt [default]` |

   Save, then use the **Register account** button on the row. Registration also
   happens automatically on first issuance, but doing it now surfaces a bad
   e-mail or CA choice before you spend a rate-limited issuance attempt. The
   **Status** column should change to *OK (registered)*.
4. **Services > ACME Client > Challenge Types**, click **+**:

   | Field | Value |
   |---|---|
   | **Enabled** | ticked |
   | **Name** | e.g. `cloudflare-dns` |
   | **Challenge Type** | `DNS-01` (already the default) |
   | **DNS Service** | `CloudFlare.com` |

   Selecting the service reveals a **Cloudflare** section with two alternatives.
   Fill in the **Restricted API Token** fields and leave the **Global API Key**
   fields (**E-Mail** and **Key**) completely empty:

   | Field | Value |
   |---|---|
   | **CF Account ID** | the Account ID from the zone Overview page |
   | **CF API Token** | the token you created |
   | **CF Zone ID (Optional)** | the Zone ID, which scopes this entry to the one domain |

   Leave **DNS Sleep Time** at `0`. That makes the plugin poll *public* DNS every
   10 seconds until the record appears. Any non-zero value switches it to a fixed
   wait *and* to querying your local resolver, which on a firewall running Unbound
   can answer differently from the internet and fail confusingly.
5. **Services > ACME Client > Certificates**, click **+**:

   | Field | Value |
   |---|---|
   | **Enabled** | ticked |
   | **Common Name** | `vpn.example.com` |
   | **ACME Account** | the account you created above |
   | **Challenge Type** | `cloudflare-dns`, the challenge type you created above |

   Leave **Alt Names** empty: a single host needs no SAN and no wildcard. Note
   that this **Challenge Type** dropdown lists *your named entries*, not
   `DNS-01`/`HTTP-01` again.
6. On the certificate row, click **Issue or renew certificate**. Watch
   **Services > ACME Client > Log Files > Acme Log** if it does not succeed; set
   **Log Level** to `debug` on the Settings page first.

The certificate then appears under **System > Trust > Certificates** as
**vpn.example.com (ACME Client)**, which is the name to look for in 4.2. The
same entry is updated in place on renewal, so the plugin keeps working.

> **The A record is still your job.** DNS-01 proves you control the zone by
> publishing a TXT record at `_acme-challenge.vpn.example.com`. It says nothing
> about where `vpn.example.com` points, and the browser callback needs a public
> **A** record aimed at the firewall's WAN address. If the zone sits behind
> Cloudflare's proxy, set that record to **DNS only** (grey cloud): the
> certificate issues perfectly either way, and only the callback breaks, which
> makes it a genuinely confusing failure.

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

### 4.3 Check the status panel

The top of the SSO settings page shows six rows. For a healthy setup:

| Row | Expected |
|---|---|
| Supervisor | running |
| SSO daemon | running |
| Management socket swap | active |
| Callback listener | listening |
| Public base URL | consistent |
| OpenVPN instance directives | present |

The *Callback listener* row only proves the service is listening on the
firewall itself; whether your users can reach it from outside is what the
firewall rules in step 5 and the DNS record are for.

If anything is off, see [Troubleshooting](#troubleshooting).

---

## Step 5: Firewall rules

Both ports are known by now, so open them in one pass.

Go to **Firewall > Rules** and select **WAN** in the interface selector at the
top of the page. Add two rules:

| | Action | Protocol | Destination | Destination Port |
|---|---|---|---|---|
| **The VPN itself** | Pass | UDP | WAN address | 1194 |
| **The browser callback** | Pass | TCP | WAN address | 9443 |

Restrict the source on the callback rule to the networks your users browse from
if you can. It only serves OAuth2 endpoints, but there is no reason to expose it
more widely than necessary.

Then select **OpenVPN** in the same interface selector. This is the interface
group that carries the rules for every OpenVPN instance, and it starts out
empty. OPNsense blocks what no rule passes, so until you add one here an
authenticated client gets a tunnel but reaches nothing through it. Add one
rule:

| Field | Value |
|---|---|
| **Action** | Pass |
| **TCP/IP Version** | IPv4 |
| **Protocol** | any |
| **Source** | the tunnel network from step 2, e.g. `10.10.10.0/24` |
| **Destination** | `LAN net` |

`LAN net` covers the single-LAN case and matches the **Local Network** example
from step 2. If you route several networks through the tunnel, put them in an
alias or add a rule per network. If you selected **Redirect gateway** in step
2, set **Destination** to `any` instead, because the clients' internet traffic
now flows through this interface too.

Routing and filtering are separate decisions: **Local Network** on the
instance tells clients the tunnel is the way to reach a network, and this rule
is what actually permits the traffic once it arrives. Tighten **Protocol**,
**Source** and **Destination** here when not every client should reach
everything; identity-based per-user rules are not possible on this hop, since
the firewall sees only tunnel IP addresses.

One full-tunnel pitfall: if redirected clients reach the LAN but not the
internet, check **Firewall > NAT > Outbound**. The default automatic mode
translates the tunnel network on its way out; in manual mode you must add an
outbound NAT rule for `10.10.10.0/24` on WAN yourself.

---

## Step 6: Connect a client

### 6.1 Export the profile

Go to **VPN > OpenVPN > Client Export** and fill in the form at the top:

| Field | Value |
|---|---|
| **Remote Access Server** | your instance, e.g. `OpenVPN SSO udp:1194` |
| **Export type** | `File Only` |
| **Hostname** | `vpn.example.com` |
| **Port** | `1194` |

**Overwrite the Hostname.** It is pre-filled with the firewall's own interface
address, and accepting that ships the raw WAN IP in everyone's profile. It also
has to match the name on your server certificate, or *Validate server subject*
rejects the connection. Do not append the port here; that is the **Port** field,
and `vpn.example.com:1194` is refused as invalid.

Leave **Validate server subject** ticked. Leave **Windows Certificate System
Store** and **Enable static challenge (OTP)** unticked: the first omits the
certificate and key from the profile, and the second injects an OTP prompt that
breaks a certificate-only profile.

Then scroll to the **Accounts / certificates** table at the bottom. There is no
dropdown: each certificate is a row, and you download by clicking the small
cloud-with-arrow icon at the right of the row you want. Click it on the
**OpenVPN client** row.

The other two rows are traps. *(none) Exclude certificate from export* produces
a profile with no `<cert>` or `<key>` at all, silently, and *OpenVPN server*
fails with *"Certificate does not belong to server CA"*, because the table lists
every certificate signed by the instance's CA rather than only client ones.

> Clicking the download icon also saves the form as that server's export
> presets. There is no separate Save button and no confirmation.

Open the file in a text editor and confirm it contains a `<cert>` block and a
`<key>` block. Those are the client's authentication method, and without them
OpenVPN refuses to start with:

```
Options error: No client-side authentication method is specified.
```

A working profile looks like this. There is no `proto` line for UDP: the
protocol rides on the `remote` line.

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

The profile carries **no** `auth-user-pass` line, and that is correct here. The
plugin puts the matching `auth-user-pass-optional` directive on the server, so
OpenVPN accepts a certificate-only client and hands the decision to the SSO
daemon. If the *OpenVPN instance directives* row on the SSO page does not read
`present`, press **Save** there before connecting: without that directive the
server rejects the client during TLS negotiation with *"Auth Username/Password
was not provided by peer"*, and no browser ever opens.

#### Optional: silence the red client log lines

Two warnings show up in red in the OpenVPN GUI log with an exported profile.
Both are cosmetic, the exporter offers no way to avoid them, and both go away
with a one-line edit of the `.ovpn`:

- `DEPRECATED OPTION: --persist-key option ignored`: the exporter writes
  `persist-key` unconditionally, and OpenVPN 2.7 ignores the option entirely.
  **Delete the `persist-key` line.**
- `WARNING: this configuration may cache passwords in memory -- use the
  auth-nocache option`: printed whenever the client touches its cached
  credentials, which in this setup are the auth token, never a password.
  **Add a line `auth-nocache`.** Pushed auth tokens are exempt from
  `auth-nocache`, so silent renewal keeps working; after the edit, confirm
  the first renegotiation (roughly an hour in) still passes without a
  browser.

### 6.2 First login

1. Import the profile into OpenVPN GUI, Tunnelblick or Viscosity.
2. Connect. Start it from the **OpenVPN GUI**, not `openvpn.exe` from a console
   or as a bare service: the GUI is what advertises browser-auth support and
   what opens the window, and the daemon refuses clients that do not advertise
   it.
3. Your default browser opens at the Microsoft sign-in page.
4. Sign in and complete MFA if your tenant requires it.
5. The browser shows a success page, and the tunnel comes up.

Reconnecting and the hourly renegotiation happen silently, without a browser
prompt: the SSO daemon renews the auth token in place, falling back to its
stored Entra refresh token when needed. The browser only returns when the
daemon cannot renew you at all, mainly after a restart of the SSO service
(its session store is in memory) or when Entra revokes the session.

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
| Plugin menu entry missing after install | The menu cache is stale. `rm -f /var/lib/php/tmp/opnsense_menu_cache.xml /var/lib/php/tmp/opnsense_acl_cache.json && service configd restart`. Do not use `service php_fpm restart`: OPNsense has no php-fpm, so it fails and, chained with `&&`, stops the restart running too. |
| Status: **management-client-auth missing** | Someone re-saved the OpenVPN instance in the GUI, which silently drops the directive. Press **Save** on the SSO page to restore it. |
| Status: **daemon not running** | Usually a bad tenant/client ID or an unreachable Entra endpoint. Check the log for the actual error. |
| Status: **callback listener not listening** | The daemon failed to bind, often a certificate problem or a port already in use. Check the log and `sockstat -l \| grep 9443`. **Do not use port 9000**: the web GUI's PHP backend (php-cgi, spawned by lighttpd) listens on `127.0.0.1:9000`, so binding the wildcard address there fails. |
| Export fails with **Client certificate not found** | Either the *"(none) Exclude certificate from export"* row was used, or the certificate has no Common Name (its **Name** column reads `/C=NL` rather than `/CN=...`). Recreate it with a Common Name. |
| Export fails with **Certificate does not belong to server CA** | You picked a *server* certificate. Export only accepts client certificates issued by the instance's CA. |
| `Options error: No client-side authentication method is specified` | The exported profile has no `<cert>`/`<key>` block, i.e. it was exported with the certificate excluded. Re-export with the certificate selected. |
| Server log: **Auth Username/Password was not provided by peer**, client times out, no browser | The instance is missing `auth-user-pass-optional`. `management-client-auth` puts OpenVPN into username/password mode, so a certificate-only profile is rejected during TLS negotiation, before the SSO daemon is consulted. Press **Save** on the SSO page to add both directives, or check the *OpenVPN instance directives* status row. |
| Client log shows red lines: **DEPRECATED OPTION: --persist-key** or **may cache passwords in memory** | Cosmetic; the exporter cannot omit either. See the profile cleanup at the end of step 6.1: delete the `persist-key` line and add `auth-nocache`. |
| Idle tunnel drops and reconnects every ~2 minutes; client log shows **Inactivity timeout (--ping-restart)**, often with **AUTH_FAILED (auth-token)** on the first retry | No keepalive on the instance, so an idle tunnel goes silent and the client's NAT mapping expires (on Windows the read error *"De opgegeven netwerknaam is niet langer beschikbaar" (code=64)* is that mapping already gone). Fix as in the next row. The token failure is a side effect of the restart; the SSO daemon re-approves silently, which is why no browser opens. |
| Server log: **WARNING: --keepalive option is missing from server config** | Harmless for authentication, but worth fixing: with no keepalive an idle UDP tunnel sends nothing and dies silently when the client's NAT mapping expires. Enable **advanced mode** on the instance and set **Keep alive interval** `10` and **Keep alive timeout** `60` (see step 2.2). Both must be set together, and the timeout must be at least twice the interval. If SSO is already live, re-saving the instance drops the plugin's directives, so press **Save** on the SSO page afterwards. |
| Client hangs at *TLS key negotiation failed to occur within 60 seconds* | The client never reaches the server, so no browser is ever requested. Capture on the OpenVPN interface with filter `1194`. If you see the request arrive and a reply leave, but the reply's destination MAC differs from the sender's, pf's `reply-to` is forcing answers to the interface gateway; tick **Disable reply-to** on the rule (enable the advanced mode toggle in the rule dialog to see it), or set it globally in Firewall > Settings > Advanced. This bites when the client shares a subnet with a gateway-bearing interface. |
| Browser never opens on connect | The client does not support browser authentication, or the profile disconnects too early (try adding `auth-retry interact`). |
| Browser opens but cannot load the page | DNS for your base URL does not point at the firewall, or the WAN rule for TCP 9443 is missing. If the zone is on Cloudflare, check the record is **DNS only** (grey cloud): proxied records resolve to Cloudflare's edge rather than your WAN, and the certificate still issues fine, so nothing else looks wrong. |
| Certificate warning in the browser | The listener certificate is self-signed or does not match the hostname in the base URL. |
| `AADSTS7000215` (invalid client secret) | Wrong secret, or the *Secret ID* was copied instead of the *Value*. |
| `AADSTS50011` (redirect URI mismatch) | The redirect URI in Entra must be exactly your base URL plus `/oauth2/callback`. |
| Browser opens about once an hour while connected; server log shows **TLS: Username/auth-token authentication failed** at the same moment | The hourly renegotiation could not renew the auth token silently. Check the *OpenVPN instance directives* status row reads `present`; the usual cause is a value in the instance's **Auth Token Lifetime** field, which blocks the plugin's `auth-gen-token ... external-auth` injection. Clear the field, save the instance, then press **Save** on the SSO page. |
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
| `supervisor.py` | `configctl openvpnauthoauth2 restart` |
| anything under `service/templates` | `configctl template reload OPNsense/OpenVPNAuthOAuth2`, then restart |
| `actions.d` | the `configd` restart above |

Useful checks on the box:

```bash
configctl openvpnauthoauth2 details
```

```bash
find /var/etc/openvpn -name 'instance-*.conf' -exec grep -H -e management-client-auth -e auth-user-pass-optional {} +
```

> **Shell note:** root's login shell is tcsh, which has no `$(...)` substitution
> and no `VAR=value command` prefix, and which treats a glob matching nothing as
> a hard error that abandons the rest of the line. Run `sh` first, as above. Note
> that `ssh root@fw '...'` still runs the remote command through tcsh no matter
> what your local shell is, so wrap remote one-liners as `ssh root@fw sh -c '...'`.

> **Do not use `service php_fpm restart`.** OPNsense has no php-fpm at all: the
> GUI is lighttpd with mod_fastcgi spawning `php-cgi`. The command fails, and in
> a chained one-liner it takes the rest of the line with it. If you ever do need
> to bounce the GUI, it is `configctl webgui restart`.

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
and pushes the result to the `gh-pages` branch that GitHub Pages serves,
together with [`tools/surfhost.conf`](tools/surfhost.conf), which is the copy
users fetch. Without `PUBLISH=1` it stages the files and prints the manual
publish commands.

> Edits to `tools/surfhost.conf` only reach users after a publish run, and then
> only once each firewall re-fetches the file: `pkg update` refreshes the
> package catalogue, not the repository configuration.

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
