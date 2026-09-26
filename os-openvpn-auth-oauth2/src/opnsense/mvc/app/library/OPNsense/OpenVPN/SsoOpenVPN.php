<?php

/*
 * Copyright (c) 2026 SurfHost.nl
 * SPDX-License-Identifier: MIT
 */

namespace OPNsense\OpenVPN;

use OPNsense\OpenVPNAuthOAuth2\OpenVPNAuthOAuth2;

/**
 * Client export type for SSO instances, offered on VPN > OpenVPN > Client
 * Export next to core's own types. Core's ExportFactory picks up every
 * IExportProvider in this directory, which is why this file lives in core's
 * library tree; the plugin package installs and removes it.
 *
 * The profile is core's "File Only" export with the edits from the SSO
 * settings page (Client profile section) applied:
 *
 * - no 'persist-tun': with it the client keeps the tunnel adapter and its
 *   pushed routes across a reconnect. When the reconnect needs a browser
 *   sign-in (expired auth token, revoked session), a full-tunnel client then
 *   has its default route in a tunnel that carries nothing yet, cannot reach
 *   the identity provider, and loops until someone disconnects it by hand.
 * - no 'persist-key': core writes it unconditionally; OpenVPN 2.7 ignores
 *   it and logs a warning about it.
 * - 'auth-nocache': without it the client logs a warning about cached
 *   passwords on every reconnect. Pushed auth tokens are exempt, so silent
 *   renewal keeps working.
 *
 * Core only writes 'auth-nocache' for instances with an authentication
 * source, and an SSO instance has none: the browser is the authentication.
 */
class SsoOpenVPN extends PlainOpenVPN
{
    /**
     * @return string plugin name
     */
    public function getName()
    {
        return gettext("File Only (SSO, openvpn-auth-oauth2)");
    }

    /**
     * @return array supported options; auth_nocache and static_challenge
     *               only act with an authentication source, which an SSO
     *               instance does not have
     */
    public function supportedOptions()
    {
        return ["plain_config", "random_local_port", "cryptoapi"];
    }

    /**
     * @return array
     */
    protected function openvpnConfParts()
    {
        $settings = (new OpenVPNAuthOAuth2())->client;
        $drop = [];
        if ((string)$settings->dropPersistTun === '1') {
            $drop[] = 'persist-tun';
        }
        if ((string)$settings->dropPersistKey === '1') {
            $drop[] = 'persist-key';
        }

        $conf = [];
        foreach (parent::openvpnConfParts() as $line) {
            if (!in_array(trim($line), $drop, true)) {
                $conf[] = $line;
            }
        }
        if ((string)$settings->authNocache === '1' && !in_array('auth-nocache', array_map('trim', $conf), true)) {
            $conf[] = 'auth-nocache';
        }

        return $conf;
    }
}
