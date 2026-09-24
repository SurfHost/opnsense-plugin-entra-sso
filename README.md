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

- [ ] Create the app registration (single tenant), or run the script in 1.6
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
- [ ] Full tunnel only: Redirect gateway `default`, DNS Servers = the firewall's LAN address
- [ ] Save, then Apply

**Plugin**

- [ ] Enable SSH in OPNsense
- [ ] Add the SurfHost repository, run `pkg update`
- [ ] Install `os-openvpn-auth-oauth2` from the Plugins page
- [ ] Fill in the SSO page: instance, tenant ID, client ID, client secret, base URL, encryption secret, TLS certificate
- [ ] Save, then check every status row is green

**Firewall**

- [ ] WAN rules: pass UDP 1194 and TCP 9443
- [ ] OpenVPN interface rule: pass tunnel network to LAN
- [ ] Full tunnel only: OpenVPN rule destination `any`, plus a Source NAT rule for the tunnel network on WAN (mode Hybrid, or Manual if already set)

**Client**

- [ ] Export the profile: File Only, real hostname, the client certificate row
- [ ] Optional: delete `persist-key`, add `auth-nocache`
- [ ] Import, connect, sign in once in the browser

---

## Step 1: Register the application in Entra ID

You are creating an application that represents your VPN, so Entra ID knows who
is asking when a user signs in.

Prefer a script? [1.6](#16-the-same-with-powershell) does 1.1 to 1.4 in one
PowerShell run and prints the values to write down.

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
3. Give it a description and an expiry (24 months is the portal's maximum;
   the PowerShell route in [1.6](#16-the-same-with-powershell) can go longer).
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

### 1.6 The same with PowerShell

Steps 1.1 to 1.4 as one script, using the Microsoft Graph PowerShell module.
Set the four variables at the top, then paste the whole block into a PowerShell
window. Sign in with an account that may grant admin consent (Application
Administrator or higher; Global Administrator always works). The first run
asks you to consent to the Graph scopes for the PowerShell module itself.

```powershell
# Module (once): Install-Module Microsoft.Graph -Scope CurrentUser
Connect-MgGraph -Scopes "Application.ReadWrite.All","AppRoleAssignment.ReadWrite.All","DelegatedPermissionGrant.ReadWrite.All","Group.Read.All"

$appName      = "OPNsense OpenVPN SSO"
$baseUrl      = "https://vpn.example.com:9443"   # public base URL, no trailing slash
$vpnGroup     = "VPN Users"                       # group allowed to connect, "" for everyone
$secretMonths = 24                                # see the note below the script

# Look up the group first, so a typo fails before anything is created.
# For a single user, replace the Get-MgGroup line with:
#   $principal = Get-MgUser -UserId "jan@example.com"
if ($vpnGroup) {
  $principal = Get-MgGroup -Filter "displayName eq '$vpnGroup'"
  if (@($principal).Count -ne 1) { throw "Expected exactly one group named '$vpnGroup'" }
}

# 1. App registration (single tenant) with the Web redirect URI
$app = New-MgApplication -DisplayName $appName -SignInAudience "AzureADMyOrg" `
  -Web @{ RedirectUris = @("$baseUrl/oauth2/callback") }

# 2. Enterprise app (service principal); Assignment required = Yes only with a group
$sp = New-MgServicePrincipal -AppId $app.AppId -AppRoleAssignmentRequired:([bool]$vpnGroup)

# 3. Delegated Graph permissions plus admin consent for the whole tenant
$scopes  = "openid","profile","offline_access","User.Read"
$graphSp = Get-MgServicePrincipal -Filter "appId eq '00000003-0000-0000-c000-000000000000'"
$access  = $graphSp.Oauth2PermissionScopes | Where-Object { $_.Value -in $scopes } |
  ForEach-Object { @{ Id = $_.Id; Type = "Scope" } }
Update-MgApplication -ApplicationId $app.Id -RequiredResourceAccess @(@{
  ResourceAppId  = $graphSp.AppId
  ResourceAccess = @($access)
})
New-MgOauth2PermissionGrant -ClientId $sp.Id -ConsentType "AllPrincipals" `
  -ResourceId $graphSp.Id -Scope ($scopes -join " ") | Out-Null

# 4. Who may connect: assign the group to the enterprise app (default access).
#    Skipped when $vpnGroup is empty: then every user in the tenant may sign in.
if ($vpnGroup) {
  New-MgServicePrincipalAppRoleAssignedTo -ServicePrincipalId $sp.Id `
    -PrincipalId $principal.Id -ResourceId $sp.Id -AppRoleId ([guid]::Empty) | Out-Null
}

# 5. Client secret
$secret = Add-MgApplicationPassword -ApplicationId $app.Id -PasswordCredential @{
  DisplayName = "OPNsense $($secretMonths)m"; EndDateTime = (Get-Date).AddMonths($secretMonths)
}

# 6. The values for Step 4; the secret is shown only this once
[pscustomobject]@{
  TenantId     = (Get-MgContext).TenantId
  ClientId     = $app.AppId
  ClientSecret = $secret.SecretText
  SecretExpiry = $secret.EndDateTime
  RedirectUri  = "$baseUrl/oauth2/callback"
} | Format-List
```

The result is identical to the portal route: the app registration and its
enterprise app both appear under the name you chose, the API permissions page
shows *Granted for &lt;tenant&gt;*, and the group is listed under **Users and
groups**.

- **No group** (`$vpnGroup = ""`) leaves *Assignment required* at No, so every
  account in the tenant can complete the sign-in; the VPN client still needs
  its certificate. You can restrict it later as described in 1.4.
- **Assigning a group** to an enterprise app needs Entra ID P1 or P2 (which
  Conditional Access needs anyway). Without it, assign users one by one with
  the `Get-MgUser` line near the top, once per user.
- **Replication delay:** if step 2 fails with *does not reference a valid
  application object*, the new app has not reached every Entra replica yet.
  Wait ten seconds and run the script again from step 2 on.
- **Secret lifetime:** the portal caps secrets at 24 months, Graph does not, so
  `$secretMonths = 60` gives five years. A tenant can still cap the lifetime
  with an app management policy, in which case step 5 fails with an error
  naming the maximum. Longer is not free: the secret sits in `config.xml` and
  in every configuration backup, and a leaked one stays valid until it
  expires or you delete it.

To **renew the secret** before it expires, add a new one, paste it on the SSO
page and save, then delete the old one:

```powershell
Connect-MgGraph -Scopes "Application.ReadWrite.All"
$app = Get-MgApplication -Filter "displayName eq 'OPNsense OpenVPN SSO'"
$app.PasswordCredentials | Format-Table DisplayName, KeyId, EndDateTime
$new = Add-MgApplicationPassword -ApplicationId $app.Id -PasswordCredential @{
  DisplayName = "OPNsense 24m"; EndDateTime = (Get-Date).AddMonths(24)
}
$new.SecretText
# after saving the new secret on the SSO page:
Remove-MgApplicationPassword -ApplicationId $app.Id -KeyId "<KeyId of the old secret>"
```

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

Click **Save**, then click **Apply** below the instance list (the page reminds
you with *After changing settings, please remember to apply them.*). **Save**
only stores the instance; **Apply** writes its configuration and starts it.
Because **Enabled** is part of the form, there is no separate step to switch it
on.

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
`management-client-auth` directive there itself. This form cannot show that
directive and drops it every time you save the instance; from then on the
plugin writes it back within seconds, and again right before every **Apply**,
so there is nothing to do on the SSO page (see
[4.4](#44-fail-closed-protection)).

#### Sending all client traffic through the VPN

By default only the **Local Network** routes are pushed, so clients reach your
LAN through the tunnel and everything else keeps going out over their own
internet connection. To send *all* their traffic through the firewall instead,
set these two fields under **Miscellaneous**:

| Field | Value |
|---|---|
| **Redirect gateway** | `default` only |
| **DNS Servers** | the firewall's LAN address, e.g. `192.168.1.1` (type it and press Enter) |

**default** is OpenVPN's `def1` flag, which is why the dropdown does not say
`def1`. It works by pushing two overriding routes rather than replacing the
client's default route, so nothing is left behind if the client disconnects
uncleanly.

**DNS Servers** matters because OPNsense pushes no DNS server on its own,
redirect gateway or not. Clients then keep their own DNS servers, and any that
are not on their local network (their provider's, or a public one such as
`8.8.8.8`) are now reached through the tunnel as well. Pushing the firewall's
address lets its Unbound resolver answer instead. Unbound accepts queries from
every network by default, so the tunnel network needs no extra entry; only if
you changed **Services > Unbound DNS > Access Lists > Default Action** to
`Deny` or `Refuse` do you need to add an allow entry for `10.10.10.0/24`.

Leave **ipv6 (default)** unselected unless the instance also has a
**Server (IPv6)** tunnel network that the firewall can route to the internet,
with IPv6 firewall rules to match. Without a **Server (IPv6)** network the
option carries no IPv6 through the tunnel: OpenVPN 2.7 clients ignore it, so
IPv6 keeps using the client's own connection, and OpenVPN 2.6 clients send
IPv6 into a tunnel that cannot carry it. If you would rather block IPv6 on
clients while they are connected, so none of it bypasses the tunnel, OpenVPN
documents a way: fill in **Server (IPv6)** with a private range such as
`fd15:53b6:dead::/64`, select **ipv6 (default)** next to **default**, and
select `block-ipv6` under **Options**. The firewall then answers the clients'
IPv6 packets with *no route to host*, so no IPv6 leaves around the tunnel.

A full tunnel also needs two firewall items in step 5: the OpenVPN rule with
**Destination** `any`, and a
[Source NAT rule](#full-tunnel-only-source-nat) for the tunnel network.
OPNsense does not create that NAT rule for an instance by itself. Without
either one, clients connect and reach the LAN, but not the internet.

Leaving **Redirect gateway** empty is a normal split tunnel.

Set these fields before you click **Save** and **Apply**, or click both again
after changing them later. If SSO is already live, there is nothing to do on
the SSO page afterwards: saving the instance drops the plugin's directives, and
the plugin restores them within seconds. Check that *Saved instance directives*
on the SSO page reads `present`.

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
- [ ] Full tunnel only: **Redirect gateway** is `default` only and **DNS
      Servers** is set, see
      [Sending all client traffic through the VPN](#sending-all-client-traffic-through-the-vpn)

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
> this repository provides, and `pkg install -r SurfHost` bypasses it. Adding
> the repository means trusting SurfHost to the same degree as any other
> package source on the firewall.

### 3.2 Install the plugin

Install from the GUI, not from the shell. Go to **System > Firmware > Plugins**
and first click **Click to view the community plugins.**, because plugins from a
third-party repository are hidden until you do. Type `os-openvpn-auth-oauth2`
in the **Name** box, click the **+** on its row, and confirm the **Third party
software** dialog with **Install**.

The row then reads *os-openvpn-auth-oauth2 (installed)*. **Tier** shows `4` and
**Repository** shows `SurfHost`; that is correct and permanent, because OPNsense
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

Since 1.5 an update restarts the SSO service by itself when it is enabled and
running: about five seconds after the package is installed, the new version
takes over. Coming from 1.4, the new SSO guard
([4.4](#44-fail-closed-protection)) checks the running instance on its first
pass: if that instance runs without SSO, it is stopped and started again with
the directives, which drops its tunnels once.

Coming from 1.4, if *SSO enforcement (running instance)* on the status panel
still reads *not watched* a minute after the update, the old version is still
running: 1.4 has no guard. Coming from a later version the panel cannot tell
the two apart, since both have one. Either way, clicking **Save** on the SSO
page, or running this in the shell, makes sure the new version runs:

```sh
configctl openvpnauthoauth2 restart
```

Disabling or removing the plugin, or selecting a different instance on the SSO
page, leaves `management-client-auth` on the instance it protected. OpenVPN
then waits for an SSO daemon that never answers and refuses every client after
about 60 seconds. This is deliberate: an instance that quietly falls back to
certificate-only logins is exactly what the plugin exists to prevent. To run
that instance without SSO, do this before removing the plugin (if you only
selected another instance on the SSO page, steps 2 and 3 are enough):

1. On the SSO page, untick **Enable** and click **Save**.
2. Open the instance under **VPN > OpenVPN > Instances** and click **Save**
   without changing anything; its Options field drops the directives.
3. Click **Apply**.

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

Stopping only the SSO service while it is enabled (for example on **System >
Diagnostics > Services**) is a different case. Apply, boot and CARP events
still restore the directives before the instance starts, but a restart of the
instance from the dashboard, the Services page or **VPN > OpenVPN > Connection
Status** is then not checked, and the status panel shows *not watched* in red.

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
| **Encryption secret** | click the gear button next to the field (any 16, 24 or 32 random letters and digits also work) |
| **Enable TLS** | ticked |
| **Certificate** | the ACME Client certificate from 4.1, listed as **vpn.example.com (ACME Client)**, or the certificate you imported instead |

The gear button creates the secret on the firewall itself, with the same
`openssl rand -hex 16` you would otherwise run by hand, and puts it in the
field. It is stored when you click **Save** below. To make one yourself
instead, run this and paste the result:

```sh
openssl rand -hex 16
```

It encrypts browser session cookies and stored refresh tokens. Changing it later
forces everyone to sign in again.

Leave **Repair OpenVPN instance directives**, in the **Advanced** section,
ticked (the default). With it on, the plugin keeps its directives on the
instance: it writes them back within seconds whenever the instance is saved,
and if the instance is ever found running without `management-client-auth`, it
stops the instance and starts it again with them. With it off, the plugin
writes nothing into the instance and keeps such an instance stopped instead.
There is no switch that turns the protection itself off while SSO is enabled.

Click **Save**. The plugin now:

- writes the daemon configuration,
- adds `management-client-auth` to your OpenVPN instance and **restarts that
  instance** (active tunnels drop once, here),
- starts the SSO service, which from then on watches the instance
  ([4.4](#44-fail-closed-protection)).

### 4.3 Check the status panel

The top of the SSO settings page shows eight rows. For a healthy setup:

| Row | Expected |
|---|---|
| Supervisor | running |
| SSO daemon | running |
| Management socket swap | active |
| Callback listener | listening |
| Public base URL | consistent |
| SSO enforcement (running instance) | enforced |
| Saved instance directives | present |
| Silent token renewal | on |

*SSO enforcement (running instance)* is the security row. It reflects the
configuration the running OpenVPN process actually loaded, not what is saved:
*enforced* means that process has `management-client-auth`, so nobody
connects without signing in. Right after the instance starts it reads
*verifying* for a few seconds, and *instance not running* while it is stopped.
A red value means the instance is, or may be, reachable with a certificate
alone, is being kept stopped, or no longer exists; see
[4.4](#44-fail-closed-protection). The small line under the label shows the
OpenVPN process ID and, when there is one, the reason.

*Saved instance directives* shows whether the directives are in the saved
configuration, which is what the next start of the instance uses. Right after
the instance is saved it briefly reads *missing, restoring*.
*Silent token renewal* reads *instance's Auth Token Lifetime in use* when that
field on the instance is not empty (see step 2.2).

While the SSO daemon runs and the enforcement row reads anything other than
*enforced* or *verifying*, the *SSO daemon* row reads *running, not
consulted*: the daemon runs, but no running instance hands it any logins.
While the instance is not running, or the guard stops, repairs or keeps it
stopped, the daemon is paused and the row reads *not running*.

The *Callback listener* row only proves the service is listening on the
firewall itself; whether your users can reach it from outside is what the
firewall rules in step 5 and the DNS record are for.

If anything is off, see [Troubleshooting](#troubleshooting).

### 4.4 Fail-closed protection

Everything SSO does hangs on one directive, `management-client-auth`. An OpenVPN
instance that runs without it never asks the SSO daemon: it lets in anyone
holding a valid client certificate for that instance, without a browser
sign-in. The instance's Options field cannot hold the directive and drops it
on every save, so up to 1.4 any save of the instance followed by **Apply** left
the instance open until **Save** was pressed on the SSO page. Since 1.5 the
plugin keeps the directive in place and does not let the instance run without
it while SSO is enabled:

- **You save the instance** under **VPN > OpenVPN > Instances.** The plugin
  writes the directives back within seconds, and again right before every
  **Apply**, boot and CARP event, so the instance is not restarted for it.
  There is nothing to do on the SSO page; *Saved instance directives* returns
  to `present` by itself.
- **The instance ever starts without them**, for example when it is restarted
  from the dashboard a second or two after it was saved. The SSO guard, part
  of the SSO service, checks the configuration every OpenVPN process started
  with, and stops such an instance within about a second, which drops every
  session it had. It then restores the directives and starts the instance
  again. It keeps the instance stopped instead, and the status panel reads
  *instance kept stopped*, when **Repair OpenVPN instance directives** is off,
  when the instance comes back without the directive after a repair, or after
  three repairs in 15 minutes.

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

#### Self-test

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
| **Version** | IPv4 |
| **Protocol** | any |
| **Source** | the tunnel network from step 2, e.g. `10.10.10.0/24` |
| **Destination** | `LAN net`, or `any` for a full tunnel |

`LAN net` covers the single-LAN case and matches the **Local Network** example
from step 2. If you route several networks through the tunnel, put them in an
alias or add a rule per network. If you selected **Redirect gateway** in step
2, **Destination** must be `any`: the clients' internet traffic now arrives on
this interface too, and `LAN net` lets only the LAN part through.

Routing and filtering are separate decisions: **Local Network** on the
instance tells clients the tunnel is the way to reach a network, and this rule
is what actually permits the traffic once it arrives. Tighten **Protocol**,
**Source** and **Destination** here when not every client should reach
everything; identity-based per-user rules are not possible on this hop, since
the firewall sees only tunnel IP addresses. Keep **Protocol** at `any` if
clients should be able to ping: `TCP/UDP` blocks ICMP, which makes a working
tunnel look broken.

### Full tunnel only: Source NAT

Skip this if you left **Redirect gateway** empty in step 2.

OPNsense's automatic Source NAT (outbound NAT) only translates the networks of
LAN-type interfaces (assigned, with an address and no gateway), the IPsec
mobile pool and legacy OpenVPN servers. It does not include the
tunnel network of an OpenVPN *instance*, so redirected traffic would leave WAN
with its private `10.10.10.x` source address and the replies never come back.
OPNsense's own road-warrior guide for instances says the same: redirect gateway
needs a manual Source NAT rule.

1. Go to **Firewall > NAT > Source NAT**.
2. Set **Mode** at the top of the page to **Hybrid Source NAT rule
   generation** and click **Apply**. Hybrid keeps the automatic rules for your
   LAN and adds your own; in **Automatic** mode the rule list is read-only and
   rules of your own are not loaded. If the mode is already **Manual Source
   NAT rule generation**, leave it as it is.
3. Click **+** and fill in:

   | Field | Value |
   |---|---|
   | **Interface** | WAN |
   | **Version** | IPv4 |
   | **Protocol** | any |
   | **Source Address** | the tunnel network, e.g. `10.10.10.0/24` |
   | **Translate Source IP** | leave empty, which uses the WAN address |
   | **Description** | e.g. `OpenVPN SSO full tunnel` |

   **Destination Address**, under the collapsed **Destination (advanced)**
   header, stays at its default `any`.
4. Click **Save**, then **Apply**.

With more than one WAN, add the same rule for each. To confirm, run this in the
firewall shell (use your own tunnel network); it must print a `nat on` line for
your WAN device:

```sh
pfctl -s nat | grep 10.10.10.
```

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
daemon. Before connecting, check that *SSO enforcement (running instance)* on
the SSO page reads `enforced` with no warning under it, and *Saved instance
directives* reads `present` (see [4.4](#44-fail-closed-protection) if not):
without `auth-user-pass-optional` the server rejects the client during TLS
negotiation with *"Auth Username/Password was not provided by peer"*, and no
browser ever opens.

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
**VPN > OpenVPN > Log File**, filtered on
`openvpn-auth-oauth2` (or `SSO guard` for the protection described in
[4.4](#44-fail-closed-protection)). From the shell:

```sh
configctl openvpnauthoauth2 details
```

| Symptom | Cause and fix |
|---|---|
| `pkg update` gives a 404 | Your ABI directory does not exist yet in the repository. Compare `pkg config abi` (OPNsense 26.7 reports `FreeBSD:15:amd64`) with the directories published in the repository. |
| Plugin menu entry missing after install | The menu cache is stale. `rm -f /var/lib/php/tmp/opnsense_menu_cache.xml /var/lib/php/tmp/opnsense_acl_cache.json && service configd restart`. Do not use `service php_fpm restart`: OPNsense has no php-fpm, so it fails and, chained with `&&`, stops the restart running too. |
| Status: **instance kept stopped** | The SSO guard found the instance running without `management-client-auth`, stopped it, and keeps it stopped. The log line `keeping OpenVPN instance ... stopped:` gives the reason and the fix. With **Repair OpenVPN instance directives** on, click **Apply** on **VPN > OpenVPN > Instances**: it restores the directives and starts the instance. **Save** on the SSO page does not start it, because the directives are usually back in the saved configuration by then, so the page finds nothing to repair. With it off, tick it and click **Save**; the GUI offers no other way to put the directives back. |
| Status: **NOT ENFORCED** or **not watched** | **NOT ENFORCED**: the running instance lacks `management-client-auth`, so a valid client certificate is enough to connect. The SSO guard stops such an instance within about a second (*cannot verify* is handled the same way). **not watched**: the SSO service is not running, or its guard is not reporting, so nothing checks the instance. In both cases start (or restart) the SSO service with the controls at the top of the SSO page. If the row stays red, stop the instance on **System > Diagnostics > Services** and check the log. |
| Status: **Saved instance directives: missing, restoring** | Normal for a few seconds after the instance was saved in **VPN > OpenVPN > Instances**: its Options field drops the directives, and the plugin writes them back. If it stays, look for `could not restore the SSO directives` in the log. The red variant *missing: the next start will be stopped* means **Repair OpenVPN instance directives** is off. |
| The instance restarts by itself right after a restart from the dashboard | The restart came within a second or two of saving the instance, before the plugin had written the directives back, so the instance started without SSO and the guard stopped it and started it again with them. One extra restart, expected; the log shows `is running WITHOUT management-client-auth`, then `enforces SSO`. |
| Status: **daemon not running** | Expected while the enforcement row reads *instance not running*, *restarting with the SSO directives*, *NOT ENFORCED, stopping instance* or *instance kept stopped*: the SSO service pauses the daemon until the instance runs with the directives. Otherwise usually a bad tenant/client ID or an unreachable Entra endpoint. Check the log for the actual error. |
| Status: **callback listener not listening** | The daemon failed to bind, often a certificate problem or a port already in use. Check the log and `sockstat -l \| grep 9443`. **Do not use port 9000**: the web GUI's PHP backend (php-cgi, spawned by lighttpd) listens on `127.0.0.1:9000`, so binding the wildcard address there fails. |
| Export fails with **Client certificate not found** | Either the *"(none) Exclude certificate from export"* row was used, or the certificate has no Common Name (its **Name** column reads `/C=NL` rather than `/CN=...`). Recreate it with a Common Name. |
| Export fails with **Certificate does not belong to server CA** | You picked a *server* certificate. Export only accepts client certificates issued by the instance's CA. |
| `Options error: No client-side authentication method is specified` | The exported profile has no `<cert>`/`<key>` block, i.e. it was exported with the certificate excluded. Re-export with the certificate selected. |
| Server log: **Auth Username/Password was not provided by peer**, client times out, no browser | The instance is missing `auth-user-pass-optional`. `management-client-auth` puts OpenVPN into username/password mode, so a certificate-only profile is rejected during TLS negotiation, before the SSO daemon is consulted. Saving the instance drops it, and there is nothing to do: the plugin restores the directives within seconds; check that *Saved instance directives* reads `present`. If the line under *SSO enforcement (running instance)* still says *auth-user-pass-optional missing*, the running instance started without it: restart it on **System > Diagnostics > Services**. |
| Client log shows red lines: **DEPRECATED OPTION: --persist-key** or **may cache passwords in memory** | Cosmetic; the exporter cannot omit either. See the profile cleanup at the end of step 6.1: delete the `persist-key` line and add `auth-nocache`. |
| Idle tunnel drops and reconnects every ~2 minutes; client log shows **Inactivity timeout (--ping-restart)**, often with **AUTH_FAILED (auth-token)** on the first retry | No keepalive on the instance, so an idle tunnel goes silent and the client's NAT mapping expires (on Windows the read error *"De opgegeven netwerknaam is niet langer beschikbaar" (code=64)* is that mapping already gone). Fix as in the next row. The token failure is a side effect of the restart; the SSO daemon re-approves silently, which is why no browser opens. |
| Server log: **WARNING: --keepalive option is missing from server config** | Harmless for authentication, but worth fixing: with no keepalive an idle UDP tunnel sends nothing and dies silently when the client's NAT mapping expires. Enable **advanced mode** on the instance and set **Keep alive interval** `10` and **Keep alive timeout** `60` (see step 2.2). Both must be set together, and the timeout must be at least twice the interval. If SSO is already live, re-saving the instance drops the plugin's directives, but there is nothing to do: the plugin restores them within seconds; check that *Saved instance directives* reads `present`. |
| Client hangs at *TLS key negotiation failed to occur within 60 seconds* | The client never reaches the server, so no browser is ever requested. Capture on the OpenVPN interface with filter `1194`. If you see the request arrive and a reply leave, but the reply's destination MAC differs from the sender's, pf's `reply-to` is forcing answers to the interface gateway; tick **Disable reply-to** on the rule (enable the advanced mode toggle in the rule dialog to see it), or set it globally in Firewall > Settings > Advanced. This bites when the client shares a subnet with a gateway-bearing interface. |
| Full tunnel: clients connect and reach the LAN, but not the internet (everything works with **Redirect gateway** empty) | One of the full-tunnel items from [step 2.2](#sending-all-client-traffic-through-the-vpn) and [step 5](#full-tunnel-only-source-nat) is missing. On the client, run `ping 1.1.1.1`. **If it fails**, the firewall drops or does not translate the traffic: in the firewall shell, `pfctl -s nat \| grep 10.10.10.` (your tunnel network) printing nothing means there is no Source NAT for the tunnel, so add the rule and check the mode is **Hybrid** or **Manual**, not **Automatic**. If it does print a `nat on` line for WAN, the OpenVPN rule's **Destination** is probably still `LAN net`: set it to `any`. Blocked packets show up under **Firewall > Log Files > Live View** as *Default deny / state violation rule*. **If the ping works** but `nslookup example.com` fails, it is DNS: set **DNS Servers** on the instance. Separately, if you selected **ipv6 (default)** without a **Server (IPv6)** network, remove it; on an OpenVPN 2.6 client its log gives this away with *OpenVPN was configured to add an IPv6 route. However, no IPv6 has been configured*. |
| Connected, DNS and the firewall's GUI work, but every ping times out | Not an SSO problem: the tunnel works, so a rule or NAT is in the way. Check in this order. The OpenVPN rule's **Protocol** must be `any` (with `TCP/UDP` ICMP is blocked), and for a full tunnel its **Destination** must be `any`. For a full tunnel, `pfctl -s nat \| grep 10.10.10.` must print a `nat on` line for WAN (see [Source NAT](#full-tunnel-only-source-nat)). Windows hosts on the LAN drop ping from other subnets by default, so test them with a TCP port instead, for example `Test-NetConnection 192.168.1.10 -Port 445` in PowerShell. **Firewall > Log Files > Live View**, filtered on the tunnel network, shows what is blocked. |
| Browser never opens on connect | The client does not support browser authentication, or the profile disconnects too early (try adding `auth-retry interact`). |
| Browser opens but cannot load the page | DNS for your base URL does not point at the firewall, or the WAN rule for TCP 9443 is missing. If the zone is on Cloudflare, check the record is **DNS only** (grey cloud): proxied records resolve to Cloudflare's edge rather than your WAN, and the certificate still issues fine, so nothing else looks wrong. |
| Certificate warning in the browser | The listener certificate is self-signed or does not match the hostname in the base URL. |
| `AADSTS7000215` (invalid client secret) | Wrong secret, or the *Secret ID* was copied instead of the *Value*. |
| `AADSTS50011` (redirect URI mismatch) | The redirect URI in Entra must be exactly your base URL plus `/oauth2/callback`. |
| Browser opens about once an hour while connected; server log shows **TLS: Username/auth-token authentication failed** at the same moment | The hourly renegotiation could not renew the auth token silently. Check the *Silent token renewal* status row: *instance's Auth Token Lifetime in use* means a value in the instance's **Auth Token Lifetime** field blocks the plugin's `auth-gen-token ... external-auth` injection. Clear the field, save the instance and click **Apply**. Nothing to do on the SSO page: the plugin restores the directives within seconds; check that *Saved instance directives* reads `present` and *Silent token renewal* reads `on`. |
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

### Fail-closed enforcement

OpenVPN only consults the SSO daemon when its configuration contains
`management-client-auth`. Without it, and with the instance's
**Authentication** field empty, OpenVPN authenticates a client on its
certificate alone. The instance form cannot hold the directive, so the plugin
writes it into the instance's configuration itself, and the form drops it again
on every save. The plugin therefore maintains one rule:

> While SSO is enabled, the protected instance may only run as an OpenVPN
> process whose loaded configuration contained `management-client-auth`. Any
> other process is stopped. Nothing ever removes the directives automatically.

OpenVPN reads that directive once, when the process starts, and keeps it for
the life of the process; OPNsense never reloads an instance in place. So the
check is per process, and three mechanisms carry it:

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

What is left open, compared with 1.4:

| Path | Up to 1.4 | 1.5 |
|---|---|---|
| Save the instance, then **Apply**; boot; CARP event | open until **Save** on the SSO page | closed: the directives are restored before the configurations are generated, and the instance is not restarted for it |
| Save the instance, then restart only that instance (dashboard, Services page, Connection Status, a cron job, an HA sync) a few seconds later | open | closed: the watcher has already restored the directives |
| The same within a second or two, with repair off, or if core stops writing the directive | open | the guard stops the process within about a second of its start; sessions it admitted die with it, and no auth token is issued to them |
| The SSO service restarts (after a crash, **Save** on the SSO page, or an update) | not applicable | a few seconds unwatched, then a check at startup; a process that was already verified stays trusted, and a repair the restart interrupted is taken up again |
| The SSO service is stopped by hand while enabled | open | Apply, boot and CARP events are still covered; other restarts are not checked, and the status panel shows *not watched* |
| Two core runs overlap (an **Apply** during a per-instance restart, say), and the second rewrites the configuration in the few milliseconds between OpenVPN reading it and writing its pidfile | open | open while that process runs: the start record then matches the rewritten file, which predates the pidfile, so a process that loaded a configuration without the directive passes as enforcing |

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

Whatever window remains, only someone holding a valid, unrevoked client
certificate issued by the instance's CA can use it: with **Authentication**
empty, core requires a verified client certificate.

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
| `supervisor.py`, `enforcement.py` | `configctl openvpnauthoauth2 restart` |
| `plugins.inc.d/openvpnauthoauth2.inc` (the pre-Apply hook) | nothing, `pluginctl` loads it on every call |
| anything under `service/templates` | `configctl template reload OPNsense/OpenVPNAuthOAuth2`, then restart |
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

> **Shell note:** root's login shell is tcsh, which has no `$(...)` substitution
> and no `VAR=value command` prefix, and which treats a glob matching nothing as
> a hard error that abandons the rest of the line. Run `sh` first, as above. Note
> that `ssh root@fw '...'` still runs the remote command through tcsh no matter
> what your local shell is, so wrap remote one-liners as `ssh root@fw sh -c '...'`.

> **Do not use `service php_fpm restart`.** OPNsense has no php-fpm at all: the
> GUI is lighttpd with mod_fastcgi spawning `php-cgi`. The command fails, and in
> a chained one-liner it takes the rest of the line with it. If you ever do need
> to bounce the GUI, it is `configctl webgui restart`.

### Core contract checklist

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
process as someone else's.
A changed pidfile path does not: the guard then sees no process at all, and
the status panel reads *instance not running* while the instance runs, so
that item matters most. The hardware tests to run after any change here, or to
the guard itself, are in
[docs/INVESTIGATION.md](docs/INVESTIGATION.md#hardware-tests-for-fail-closed-enforcement).

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
