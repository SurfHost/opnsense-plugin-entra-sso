# OpenVPN single sign-on with Microsoft Entra ID for OPNsense

Sign in to OpenVPN on OPNsense with a Microsoft work account. Users connect
with their normal OpenVPN client, a browser tab opens at the Microsoft sign-in
page (MFA and Conditional Access apply), and the tunnel comes up; reconnects
are silent. The plugin `os-openvpn-auth-oauth2` packages
[openvpn-auth-oauth2](https://github.com/jkroepke/openvpn-auth-oauth2) for one
OpenVPN instance per firewall; other instances keep working without SSO.

## Contents

1. [What you need](#what-you-need)
2. [Quick checklist](#quick-checklist)
3. [Step 1: Register the application in Entra ID](#step-1-register-the-application-in-entra-id)
4. [Step 2: Prepare the OpenVPN server](#step-2-prepare-the-openvpn-server)
5. [Step 3: Install the plugin](#step-3-install-the-plugin)
6. [Step 4: Configure the plugin](#step-4-configure-the-plugin)
7. [Step 5: Firewall rules](#step-5-firewall-rules)
8. [Step 6: Connect a client](#step-6-connect-a-client)
9. [Maintenance](#maintenance)
10. [Troubleshooting](#troubleshooting)
11. [How it works](#how-it-works)

## What you need

| | |
|---|---|
| **Firewall** | OPNsense 26.7 or newer |
| **Entra ID** | Rights to create an app registration (Application Administrator or higher) |
| **DNS** | A public hostname for the firewall's WAN address, e.g. `vpn.example.com` |
| **Certificate** | A browser-trusted certificate for that hostname (Let's Encrypt via **os-acme-client**, see 4.1) |
| **Ports** | UDP 1194 (OpenVPN) and TCP 9443 (browser callback), reachable from the internet |
| **Client** | OpenVPN GUI 2.6+ (Windows), Tunnelblick 4.0.0b10+ (macOS), Viscosity, or OpenVPN3 3.9+ |

## Quick checklist

**Entra ID** (step 1, or the script in 1.6)

- [ ] App registration, single tenant, Web redirect URI
      `https://vpn.example.com:9443/oauth2/callback`
- [ ] Client secret created, its value copied
- [ ] Permissions `openid`, `profile`, `offline_access`, admin consent granted
- [ ] Enterprise app: *Assignment required* = Yes, users assigned
- [ ] Tenant ID and client ID noted

**OpenVPN instance** (step 2)

- [ ] CA, server certificate and client certificate, each with a Common Name
- [ ] Server instance: UDP 1194, tunnel network, local network, keepalive
      `10`/`60`
- [ ] Authentication, Auth Token Lifetime and Renegotiate time empty
- [ ] Save, then Apply

**Plugin** (steps 3 and 4)

- [ ] Root shell open, SurfHost repository added, `pkg update`
- [ ] `os-openvpn-auth-oauth2` installed from the Plugins page
- [ ] DNS A record and Let's Encrypt certificate for the VPN hostname
- [ ] SSO page filled in and saved, every status row green

**Firewall** (step 5)

- [ ] WAN: pass UDP 1194 and TCP 9443
- [ ] OpenVPN: pass the tunnel network to LAN
- [ ] Full tunnel only: Redirect gateway, DNS Servers, rule destination `any`,
      Source NAT rule

**Client** (step 6)

- [ ] Profile exported: File Only, real hostname, client certificate row
- [ ] Imported, connected, signed in once in the browser

---

## Step 1: Register the application in Entra ID

Prefer a script? [1.6](#16-the-same-with-powershell) does 1.1 to 1.4 in one
PowerShell run.

### 1.1 Create the app registration

1. In the [Microsoft Entra admin center](https://entra.microsoft.com), go to
   **Entra ID > App registrations** and click **New registration**.
2. Enter a **Name** (e.g. `OPNsense OpenVPN SSO`), set **Supported account
   types** to *Single tenant only - &lt;your tenant&gt;*, and click
   **Register**.
3. Go to **Manage > Authentication**. On the **Redirect URI configuration**
   tab, click **Add Redirect URI**, choose the **Web** tile and enter, with
   your own hostname:
   ```
   https://vpn.example.com:9443/oauth2/callback
   ```
   Click **Configure**.
4. On the **Overview** page, copy the **Application (client) ID** and the
   **Directory (tenant) ID**.

### 1.2 Create a client secret

1. Go to **Certificates & secrets** and, under **Client secrets**, click **New
   client secret**.
2. Choose an expiry (24 months at most here; 1.6 can go longer) and click
   **Add**.
3. Copy the **Value** column now. It is shown only once, and the **Secret ID**
   is not what you need.
4. Put the expiry date in your calendar: every VPN login fails once the secret
   expires ([renewing](#renewing-the-secret)).

### 1.3 Check API permissions

1. Go to **API permissions**. The delegated Microsoft Graph permissions
   **openid**, **profile** and **offline_access** must be listed; add missing
   ones with **Add a permission > Microsoft Graph > Delegated permissions**.
   `offline_access` is what makes reconnects silent.
2. Click **Grant admin consent for &lt;tenant&gt;**.

### 1.4 Decide who may connect

1. Go to **Entra ID > Enterprise apps > All applications** and open the app.
2. Set **Properties > Assignment required?** to **Yes** and click **Save**.
3. Under **Users and groups > Add user/group**, assign the people or groups who
   may use the VPN. Microsoft refuses everyone else during sign-in.

### 1.5 Optional: Conditional Access

Under **Entra ID > Conditional Access > Policies**, create a policy scoped to
this application to require MFA, a compliant device or named locations.

### 1.6 The same with PowerShell

Steps 1.1 to 1.4 in one run, with the Microsoft Graph PowerShell module. Set
the four variables at the top, paste the whole block into PowerShell, and sign
in as Application Administrator or higher. The first run also asks consent for
the module itself.

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

- **No group** (`$vpnGroup = ""`): every account in the tenant can sign in
  (the VPN client still needs its certificate); restrict it later with 1.4.
- **Without Entra ID P1 or P2** you cannot assign a group. Replace the
  `Get-MgGroup` line with the `Get-MgUser` line for the first user (keep
  `$vpnGroup` non-empty), and add further users in the portal under **Users
  and groups** (1.4).
- **Secret lifetime:** Graph is not capped at 24 months, so
  `$secretMonths = 60` gives five years, unless a tenant policy sets a lower
  maximum.
- **Replication delay:** if it stops at `# 2. Enterprise app` with `does not
  reference a valid application object`, wait ten seconds and run it again
  from `# 2.` on, not the whole block.

### What to write down

| Value | Where you found it | Example |
|---|---|---|
| Directory (tenant) ID | App registration > Overview | `2c9f...-...-...-...-...b81e` |
| Application (client) ID | App registration > Overview | `7a1b...-...-...-...-...4f3d` |
| Client secret value | Certificates & secrets | `abc8Q~...` |
| Public base URL | Chosen by you | `https://vpn.example.com:9443` |

---

## Step 2: Prepare the OpenVPN server

Already have an OpenVPN server instance? Check it against
[2.3](#23-checklist-for-an-existing-instance) and go to step 3.

### 2.1 Certificates

1. Go to **System > Trust > Authorities**, click **Add** and create the CA with
   the values below.
2. Go to **System > Trust > Certificates**, click **Add** and create the server
   certificate, then click **Add** again for the client certificate, with the
   values below.

| Field | CA | Server certificate | Client certificate |
|---|---|---|---|
| **Method** | `Create an internal Certificate Authority` | `Create an internal Certificate` | `Create an internal Certificate` |
| **Description** | `OpenVPN` | `OpenVPN server` | `OpenVPN client` |
| **Type** | | `Server Certificate` | `Client Certificate` |
| **Issuer** | | `OpenVPN` | `OpenVPN` |
| **Common Name** | `OpenVPN` | `vpn.example.com` | `vpn` |

3. In the certificate list, the client certificate's **Name** must read
   `/CN=vpn`. If it reads only `/C=NL`, the Common Name is missing and the
   export in step 6 fails.

### 2.2 Create the instance

1. Go to **VPN > OpenVPN > Instances** and click **+**.
2. Switch on **advanced mode** (the toggle at the top of the dialog); the keep
   alive fields only appear there.
3. Fill in:

   | Field | Value |
   |---|---|
   | *General Settings* | |
   | **Role** | `Server` |
   | **Description** | e.g. `OpenVPN SSO` |
   | **Enabled** | ticked |
   | **Protocol** | `UDP` |
   | **Port number** | `1194` |
   | **Type** | `TUN` |
   | **Server (IPv4)** | a free subnet for VPN clients, e.g. `10.10.10.0/24` |
   | *Trust* | |
   | **Certificate** | `OpenVPN server` from 2.1 |
   | **Certificate Authority** | empty (taken from the certificate) |
   | **Verify Client Certificate** | `require` (the default) |
   | *Authentication* | |
   | **Authentication** | empty |
   | **Auth Token Lifetime** | empty |
   | **Renegotiate time** | empty |
   | *Routing* | |
   | **Local Network** | the networks clients should reach, e.g. `192.168.1.0/24` |
   | *Keep alive* | |
   | **Keep alive interval** | `10` |
   | **Keep alive timeout** | `60`; without keepalive an idle tunnel drops about every two minutes |

4. Click **Save**, then **Apply** below the instance list. **Save** alone does
   not start the instance.

### 2.3 Checklist for an existing instance

Open the instance under **VPN > OpenVPN > Instances** and check:

- [ ] **Authentication** is empty (otherwise users need that backend as well
      as Entra ID)
- [ ] **Auth Token Lifetime** is empty (a value brings the browser back every
      hour); **Renegotiate time** is empty or non-zero
- [ ] **Verify Client Certificate** is `require`, and users have client
      certificates
- [ ] **Keep alive interval** and **Keep alive timeout** are set (advanced
      mode), e.g. `10` and `60`

**Note:** once you save the SSO page in step 4, every client of this instance
must sign in through the browser. Clients without browser support, such as
NetworkManager, can no longer connect.

---

## Step 3: Install the plugin

### 3.1 Add the SurfHost repository

1. Open a root shell: choose **8) Shell** on the console, or enable SSH under
   **System > Settings > Administration**, section **Secure Shell** (tick
   **Enable Secure Shell**, **Permit root user login** and **Permit password
   login**, and click **Save**; untick them afterwards if you only need SSH for
   this) and log in from the LAN as `root`.
2. Run:

   ```sh
   fetch -o /usr/local/etc/pkg/repos/surfhost.conf https://surfhost.github.io/opnsense-plugin-entra-sso/surfhost.conf
   pkg update
   ```

### 3.2 Install the plugin

Install from the GUI: a plugin installed with `pkg install` is not reinstalled
after a configuration restore.

1. Go to **System > Firmware > Plugins** and click **Click to view the
   community plugins.** Third-party plugins stay hidden until you do.
2. Type `os-openvpn-auth-oauth2` in the **Name** box, click **+** on its row,
   and confirm the **Third party software** dialog with **Install**.
3. The row now reads *os-openvpn-auth-oauth2 (installed)*, and the menu has a
   new entry **VPN > OpenVPN > SSO (OAuth2 / Entra ID)**. If the entry is
   missing, clear the menu cache in the shell:

   ```bash
   rm -f /var/lib/php/tmp/opnsense_menu_cache.xml /var/lib/php/tmp/opnsense_acl_cache.json && service configd restart
   ```

---

## Step 4: Configure the plugin

### 4.1 Certificate for the callback listener

1. Create a public DNS **A** record for `vpn.example.com` pointing at the WAN
   address. On Cloudflare, set it to **DNS only** (grey cloud), or the browser
   callback fails.
2. The listener at `https://vpn.example.com:9443` needs a publicly trusted
   certificate for that hostname (not the OpenVPN server certificate from
   2.1). Already have one? Import it under **System > Trust > Certificates**
   and go to 4.2. Otherwise get one from Let's Encrypt with
   **os-acme-client**, validated over DNS at Cloudflare, as below.

**Cloudflare API token**

1. In the Cloudflare dashboard, go to **My Profile > API Tokens**, click
   **Create Token**, then **Create Custom Token**.
2. Give it exactly these two permissions:

   | Group | Resource | Level |
   |---|---|---|
   | Zone | DNS | Edit |
   | Zone | Zone | Read |

3. Under **Zone Resources**, choose **Include > Specific zone >** your domain.
4. Click **Continue to summary** and **Create Token**, and copy the token. It
   is shown only once.
5. On your zone's **Overview** page, copy the **Zone ID** and **Account ID**
   from the **API** panel.

**os-acme-client**

1. Install `os-acme-client` under **System > Firmware > Plugins**.
2. Under **Services > ACME Client > Settings**, tick **Enable Plugin** and
   click **Apply**.
3. Under **Services > ACME Client > Accounts**, click **+** and fill in:

   | Field | Value |
   |---|---|
   | **Name** | e.g. `letsencrypt` |
   | **E-Mail Address** | your address, for expiry warnings |
   | **ACME CA** | `Let's Encrypt [default]` |

   Click **Save**, then **Register account** on the row. **Status** changes to
   *OK (registered)*.
4. Under **Services > ACME Client > Challenge Types**, click **+** and fill in.
   Choosing the DNS service reveals the **Cloudflare** fields; use the
   **Restricted API Token** ones:

   | Field | Value |
   |---|---|
   | **Enabled** | ticked |
   | **Name** | e.g. `cloudflare-dns` |
   | **Challenge Type** | `DNS-01` (the default) |
   | **DNS Service** | `CloudFlare.com` |
   | **CF Account ID** | the Account ID |
   | **CF API Token** | the token |
   | **CF Zone ID (Optional)** | the Zone ID |
   | **E-Mail**, **Key** (Global API Key) | empty |
   | **DNS Sleep Time** | `0` |

   Click **Save**.
5. Under **Services > ACME Client > Certificates**, click **+** and fill in:

   | Field | Value |
   |---|---|
   | **Enabled** | ticked |
   | **Common Name** | `vpn.example.com` |
   | **Alt Names** | empty |
   | **ACME Account** | the account you registered, e.g. `letsencrypt` |
   | **Challenge Type** | the challenge type you created, e.g. `cloudflare-dns` |

   Click **Save**.
6. Click **Issue or renew certificate** on the row. If it fails, set **Log
   Level** to `debug` on the Settings page and read the **Acme Log** under
   **Services > ACME Client > Log Files**.

The certificate appears under **System > Trust > Certificates** as
**vpn.example.com (ACME Client)** and is renewed in place.

### 4.2 Fill in the settings

Go to **VPN > OpenVPN > SSO (OAuth2 / Entra ID)**, fill in and click **Save**:

| Field | Value |
|---|---|
| **Enable** | ticked |
| **OpenVPN instance** | the instance from step 2 |
| **Tenant ID** | Directory (tenant) ID from step 1 |
| **Client ID** | Application (client) ID from step 1 |
| **Client secret** | the secret **Value** from step 1 |
| **Allowed groups** | empty (1.4 decides who may connect; group IDs here need a groups claim in Entra, see [runbook step 4](docs/INVESTIGATION.md#entra-id-app-registration-runbook)) |
| **Public base URL** | `https://vpn.example.com:9443`, exactly the redirect URI without `/oauth2/callback` |
| **Listen port** | `9443` |
| **Encryption secret** | click the gear button next to the field, or paste the output of `openssl rand -hex 16` |
| **Enable TLS** | ticked |
| **Certificate** | **vpn.example.com (ACME Client)** from 4.1, or the certificate you imported |
| **Repair OpenVPN instance directives** (section **Advanced**) | ticked (the default) |

Saving starts the SSO service and restarts the instance once with the SSO
directives, which drops its active tunnels.

### 4.3 Check the status panel

The top of the SSO page shows eight rows. For a working setup they read:

| Row | Expected |
|---|---|
| **Supervisor** | running |
| **SSO daemon** | running |
| **Management socket swap** | active |
| **Callback listener** | listening |
| **Public base URL** | consistent |
| **SSO enforcement (running instance)** | enforced (*verifying* for a few seconds after a start) |
| **Saved instance directives** | present (*missing, restoring* for a few seconds after the instance is saved) |
| **Silent token renewal** | on |

Anything else: see [Troubleshooting](#troubleshooting).

### 4.4 Fail-closed protection

Nothing to do here. When you save the instance later (as in 5.3), the plugin
writes its SSO directives back within seconds. If the instance ever runs
without them, the SSO service restarts it with them, or keeps it stopped while
**Repair OpenVPN instance directives** is off.
[Details and a self-test](docs/HOW-IT-WORKS.md#fail-closed-enforcement).

---

## Step 5: Firewall rules

### 5.1 WAN

Go to **Firewall > Rules**, select **WAN** in the interface selector at the top,
and add two rules:

| | Action | Protocol | Destination | Destination Port |
|---|---|---|---|---|
| **The VPN itself** | Pass | UDP | WAN address | 1194 |
| **The browser callback** | Pass | TCP | WAN address | 9443 |

If you can, restrict the callback rule's source to the networks your users
browse from.

### 5.2 OpenVPN

Select **OpenVPN** in the same selector. It starts empty, and without a rule
here clients get a tunnel but reach nothing. Add one rule:

| | Action | Version | Protocol | Source | Destination |
|---|---|---|---|---|---|
| **VPN clients** | Pass | IPv4 | any | `10.10.10.0/24` | `LAN net` |

Use your own tunnel network from step 2 as **Source**. Keep **Protocol** at
`any`, because `TCP/UDP` blocks ping. For several networks, use an alias or
one rule per network; for a full tunnel (5.3), set **Destination** to `any`.

### 5.3 Optional: full tunnel

Sends all client traffic, not just the LAN, through the firewall.

1. Open the instance under **VPN > OpenVPN > Instances** and set, under
   **Miscellaneous**:

   | Field | Value |
   |---|---|
   | **Redirect gateway** | `default` only |
   | **DNS Servers** | the firewall's LAN address, e.g. `192.168.1.1` (type it and press Enter) |

   Leave **ipv6 (default)** unselected unless the instance has a **Server
   (IPv6)** tunnel network that the firewall routes to the internet, with IPv6
   rules to match ([background](docs/HOW-IT-WORKS.md#full-tunnel)). Click
   **Save**, then **Apply**.
2. Set the **Destination** of the OpenVPN rule from 5.2 to `any`.
3. Go to **Firewall > NAT > Source NAT**, set **Mode** to **Hybrid Source NAT
   rule generation** and click **Apply**. If it already is **Manual Source NAT
   rule generation**, leave it.
4. Click **+** and add this rule, with your own tunnel network as **Source
   Address**:

   | | Interface | Version | Protocol | Source Address | Destination Address | Translate Source IP |
   |---|---|---|---|---|---|---|
   | **OpenVPN SSO full tunnel** | WAN | IPv4 | any | `10.10.10.0/24` | any | empty |

   An empty **Translate Source IP** uses the WAN address. With several WANs,
   add one rule per WAN. Click **Save**, then **Apply**.
5. In the firewall shell, check that this prints a `nat on` line for your WAN
   device (use your own tunnel network):

   ```sh
   pfctl -s nat | grep 10.10.10.
   ```

---

## Step 6: Connect a client

### 6.1 Export the profile

1. Go to **VPN > OpenVPN > Client Export** and fill in the form at the top:

   | Field | Value |
   |---|---|
   | **Remote Access Server** | your instance, e.g. `OpenVPN SSO udp:1194` |
   | **Export type** | `File Only` |
   | **Hostname** | `vpn.example.com`, replacing the pre-filled address; no port here |
   | **Port** | `1194` |
   | **Validate server subject** | ticked |
   | **Windows Certificate System Store** | unticked |
   | **Enable static challenge (OTP)** | unticked |

2. In the **Accounts / certificates** table at the bottom, click the download
   icon (a cloud with an arrow) on the **OpenVPN client** row. Do not use the
   *(none)* or *OpenVPN server* rows.
3. Open the file and check that it has a `<cert>` and a `<key>` block. It has
   no `auth-user-pass` line; that is correct here.

### 6.2 First login

1. Import the profile into OpenVPN GUI, Tunnelblick or Viscosity.
2. Connect. On Windows, use the **OpenVPN GUI**, not `openvpn.exe` or a
   service: only the GUI opens the browser.
3. Sign in on the Microsoft page that opens, with MFA if your tenant requires
   it. The browser shows a success page and the tunnel comes up.

Reconnects are silent. The browser mainly returns after a restart of the SSO
service or when Entra revokes the session.

---

## Maintenance

### Updating and removing

**Update.** Run the update under **System > Firmware > Updates**. The SSO
service restarts by itself about five seconds later. If *SSO enforcement
(running instance)* still reads *not watched* a minute after the update, click
**Save** on the SSO page or run:

```sh
configctl openvpnauthoauth2 restart
```

**Run the instance without SSO.** Disabling or removing the plugin, or
selecting another instance on the SSO page, leaves the SSO directives on the
instance, and OpenVPN then refuses every client. To clear them:

1. On the SSO page, untick **Enable** and click **Save**. (If you only selected
   another instance, skip this step and do 2 and 3 on the old instance.)
2. Open the instance under **VPN > OpenVPN > Instances** and click **Save**
   without changing anything. This drops the directives.
3. Click **Apply**.

**Remove the plugin.** Clear the directives as above first, then:

1. On **System > Firmware > Plugins**, use the remove action on its row (not
   `pkg delete`).
2. Remove the daemon and the repository in the shell:

   ```sh
   pkg delete openvpn-auth-oauth2
   rm /usr/local/etc/pkg/repos/surfhost.conf
   pkg update
   ```

### Renewing the secret

Add a new secret, paste it on the SSO page and click **Save**, then delete the
old one:

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

The tenant ID, client ID and secret carry over to a new or rebuilt firewall.
Check that:

- the new public base URL plus `/oauth2/callback` is a redirect URI in Entra
  (add it on **Authentication** if the hostname or port changed);
- DNS for the hostname points at the new firewall;
- the listener certificate from 4.1 exists on the new firewall (re-issue or
  import it).

---

## Troubleshooting

Start with the status panel on the SSO page and the log at **VPN > OpenVPN >
Log File**, filtered on `openvpn-auth-oauth2`. In the shell:

```sh
configctl openvpnauthoauth2 details
```

| Symptom | Fix |
|---|---|
| **Entra ID and install** | |
| PowerShell script stops at `# 2. Enterprise app` with `does not reference a valid application object` | The new app has not replicated yet. Wait ten seconds and run the script again from `# 2.` on. |
| `pkg update` gives a 404 | The repository has no build for your ABI yet. Compare `pkg config abi` (OPNsense 26.7: `FreeBSD:15:amd64`) with the directories in the repository. |
| Plugins page shows *(misconfigured)* | It was installed with `pkg install`. Repair it under **System > Firmware > Status** with **Resolve plugin conflicts > Reset all local conflicts**, or with `configctl firmware resync`. |
| SSO menu entry missing after install | Clear the menu cache with the command in [3.2](#32-install-the-plugin). |
| **Status panel** | |
| **SSO enforcement**: *NOT ENFORCED*, *cannot verify* or *not watched* | A valid client certificate may be enough to connect, or nothing is watching the instance. Start or restart the SSO service with the controls at the top of the SSO page. If the row stays red, stop the instance on **System > Diagnostics > Services** and check the log. |
| **SSO enforcement**: *instance kept stopped* | The log line `keeping OpenVPN instance ... stopped` says why. With **Repair OpenVPN instance directives** on, click **Apply** on **VPN > OpenVPN > Instances** (**Save** on the SSO page does not start it). With it off, tick it and click **Save**. |
| **Saved instance directives** stays *missing, restoring* | Search the log for `could not restore the SSO directives`. The red *missing: the next start will be stopped* means **Repair OpenVPN instance directives** is off; tick it and click **Save**. |
| The instance restarts once by itself after a restart from the dashboard | Expected when that restart came within seconds of saving the instance: it started without SSO, and the guard restarted it with the directives. |
| **SSO daemon**: *not running* | Expected while the instance is stopped or being repaired. Otherwise usually a wrong tenant or client ID, or Entra is unreachable: check the log. |
| **Callback listener**: *not listening* | Often a certificate problem or a port already in use: check the log and `sockstat -l \| grep 9443`. Do not use port 9000. |
| Browser opens about once an hour while connected | **Silent token renewal** reads *instance's Auth Token Lifetime in use*: clear that field on the instance, then **Save** and **Apply**. It reads *off*: tick **Repair OpenVPN instance directives** and click **Save**. |
| **Export and client** | |
| Export: `Client certificate not found` | You used the *(none)* row, or the certificate has no Common Name (its **Name** reads `/C=NL`). Recreate it with one. |
| Export: `Certificate does not belong to server CA` | You used the server certificate row; use the **OpenVPN client** row. |
| `Options error: No client-side authentication method is specified` | The profile has no `<cert>`/`<key>` block. Re-export from the **OpenVPN client** row. |
| Client log in red: `DEPRECATED OPTION: --persist-key` or `may cache passwords in memory` | Harmless. To hide them, delete the `persist-key` line from the profile and add a line `auth-nocache`. |
| Client hangs at `TLS key negotiation failed to occur within 60 seconds` | The client does not reach the server: check the WAN rule for UDP 1194. If a packet capture (filter `1194`) shows replies going to a different MAC than the requests came from, tick **Disable reply-to** on the rule (advanced mode in the rule dialog), or globally under **Firewall > Settings > Advanced**. |
| Server log: `Auth Username/Password was not provided by peer`, no browser | The instance lacks `auth-user-pass-optional`. Check that **Saved instance directives** reads *present*. If the line under **SSO enforcement** says *auth-user-pass-optional missing*, restart the instance on **System > Diagnostics > Services**. |
| Idle tunnel drops about every 2 minutes (`Inactivity timeout (--ping-restart)`), or server log `--keepalive option is missing` | Set **Keep alive interval** `10` and **Keep alive timeout** `60` on the instance (advanced mode), then **Save** and **Apply**. |
| Browser never opens | The client lacks browser support (on Windows use the **OpenVPN GUI**), or the profile gives up too early: add `auth-retry interact`. |
| **Sign-in** | |
| Browser opens but cannot load the page | DNS for the base URL does not point at the WAN address (on Cloudflare: **DNS only**, not proxied), or the WAN rule for TCP 9443 is missing. |
| Certificate warning in the browser | The listener certificate is self-signed or does not match the hostname in the base URL. |
| `AADSTS7000215` (invalid client secret) | Wrong secret, or the **Secret ID** was copied instead of the **Value**. |
| `AADSTS50011` (redirect URI mismatch) | The redirect URI in Entra must be exactly the base URL plus `/oauth2/callback`. |
| Sign-in succeeds but the VPN is refused | The user is not assigned to the enterprise app (1.4), or not in **Allowed groups**. |
| Everyone is suddenly refused | The client secret expired: [renew it](#renewing-the-secret). |
| **Full tunnel and routing** | |
| Full tunnel: LAN works, but `ping 1.1.1.1` from the client fails | `pfctl -s nat \| grep 10.10.10.` must print a `nat on` line for WAN (Source NAT mode **Hybrid** or **Manual**, see [5.3](#53-optional-full-tunnel)), and the OpenVPN rule's **Destination** must be `any`. |
| Full tunnel: `ping 1.1.1.1` works, `nslookup example.com` fails | Set **DNS Servers** on the instance. If **Services > Unbound DNS > Access Lists > Default Action** is `Deny` or `Refuse`, allow the tunnel network there. |
| Full tunnel: IPv6 fails on the client | Unselect **ipv6 (default)** unless the instance has a **Server (IPv6)** network. |
| Connected, DNS works, but pings time out | The OpenVPN rule's **Protocol** must be `any`. Windows hosts drop ping from other subnets: test with `Test-NetConnection 192.168.1.10 -Port 445`. **Firewall > Log Files > Live View** shows what is blocked. |

---

## How it works

Clients connect with a certificate-only profile. OpenVPN hands each login to
the `openvpn-auth-oauth2` daemon over its management interface; the daemon
sends the client a sign-in URL, the user signs in to Entra ID in a browser,
and the daemon approves the session with an auth token that keeps reconnects
silent. OPNsense's Connection Status page already uses that management socket,
so the plugin moves the socket aside and lets the daemon pass the GUI's
requests through. The plugin also keeps its directives on the instance and
stops the instance if it runs without them.

- [docs/HOW-IT-WORKS.md](docs/HOW-IT-WORKS.md): fail-closed enforcement, the
  status panel and log lines in detail, a self-test, background to the steps
- [docs/INVESTIGATION.md](docs/INVESTIGATION.md): design, rejected
  alternatives, known limitations
- [docs/MAINTAINING.md](docs/MAINTAINING.md): development, building and
  publishing releases

## License

MIT, see [LICENSE](LICENSE). The plugin packages
[openvpn-auth-oauth2](https://github.com/jkroepke/openvpn-auth-oauth2) by Jan-Otto
Kröpke, also MIT licensed.
