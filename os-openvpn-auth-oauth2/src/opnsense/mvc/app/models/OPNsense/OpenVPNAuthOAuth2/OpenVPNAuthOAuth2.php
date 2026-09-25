<?php

/*
 * Copyright (c) 2026 SurfHost.nl
 * SPDX-License-Identifier: MIT
 */

namespace OPNsense\OpenVPNAuthOAuth2;

use OPNsense\Base\BaseModel;
use OPNsense\Base\Messages\Message;
use OPNsense\Core\Config;

class OpenVPNAuthOAuth2 extends BaseModel
{
    /**
     * The directives OpenVPN needs before it defers client connects to a
     * management client. Core's various_flags is a closed OptionField that
     * offers neither, so the GUI cannot set them and silently drops them when
     * the instance is saved.
     *
     * 'management-client-auth' hands the connect decision to the SSO daemon.
     *
     * 'auth-user-pass-optional' is not optional despite the name: enabling
     * management-client-auth puts OpenVPN into username/password mode, so
     * without it the server demands credentials that a certificate-only
     * profile never sends and kills the session during TLS negotiation with
     * "Auth Username/Password was not provided by peer". That happens before
     * the management interface is consulted, so the daemon never sees the
     * client, never issues a WEB_AUTH url, and no browser ever opens.
     */
    public const REQUIRED_FLAGS = ['management-client-auth', 'auth-user-pass-optional'];

    /**
     * Largest logo for the page after sign-in, in bytes of image. It is
     * stored as a data URI in config.xml, so it also lands in every config
     * backup and in each page the daemon serves.
     */
    public const LOGO_MAX_BYTES = 65536;

    /**
     * The auth token directive injected alongside REQUIRED_FLAGS.
     * 'external-auth' makes OpenVPN hand a presented token to the SSO daemon
     * for validation instead of judging it itself; together with the daemon's
     * refresh.use-session-id this renews sessions silently at each
     * renegotiation. Without it, OpenVPN rejects the token at the first
     * renegotiation and the client falls back to a full reconnect with a
     * browser round-trip roughly once an hour.
     */
    public function tokenDirective()
    {
        return sprintf('auth-gen-token %d external-auth', (int)(string)$this->daemon->authTokenLifetime);
    }

    /**
     * Add every REQUIRED_FLAGS entry, plus the tokenDirective() line, to the
     * selected instance's various_flags directly in config.xml, bypassing the
     * closed OptionField the GUI enforces. The OpenVPN config generator emits
     * various_flags entries verbatim as their own directive lines (spaces
     * included), so the values take effect on the next instance restart.
     *
     * The token directive is only injected while the instance's own
     * 'Auth Token Lifetime' field is empty: both produce an auth-gen-token
     * line, and OpenVPN refuses to start on the duplicate. Stale or
     * conflicting auth-gen-token entries are removed either way, including
     * a previously injected one after the user fills the field.
     *
     * Deliberately writing into core's configuration section: there is no
     * supported injection point until core accepts the directives (see
     * docs/INVESTIGATION.md). Guarded by a model toggle.
     *
     * Callers: the SSO page (reconfigure, start and restart), the 'crl'
     * configure hook that core runs right before it regenerates the instance
     * configs, and the SSO guard through
     * 'pluginctl -c openvpnauthoauth2_directives'. The whole read-modify-write
     * runs under the Config lock, so it must not be called while the caller
     * already holds that lock. The append order is stable, so core
     * regenerates a byte-identical instance config after a repair and does
     * not restart the instance for it.
     *
     * @return bool|null true when config.xml was changed and saved (the
     *                   running instance only picks it up at its next start,
     *                   which is up to the caller), false when everything was
     *                   already in place, null when not applicable
     */
    public function ensureClientAuthFlag()
    {
        if (
            (string)$this->general->enabled !== '1' ||
            (string)$this->daemon->autoFixInstanceFlag !== '1'
        ) {
            return null;
        }
        $uuid = (string)$this->general->vpnInstance;
        if ($uuid === '') {
            return null;
        }

        $cfg = Config::getInstance();
        try {
            // flock plus reload, so an instance save that landed after this
            // model was loaded is neither lost nor overwritten
            $cfg->lock();
            $config = $cfg->object();
            if (!isset($config->OPNsense->OpenVPN->Instances->Instance)) {
                return null;
            }

            foreach ($config->OPNsense->OpenVPN->Instances->Instance as $instance) {
                if ((string)$instance['uuid'] !== $uuid) {
                    continue;
                }
                $flags = array_values(array_filter(
                    array_map('trim', explode(',', (string)$instance->various_flags)),
                    'strlen'
                ));
                $changed = false;

                $missing = array_diff(self::REQUIRED_FLAGS, $flags);
                if ($missing !== []) {
                    $flags = array_merge($flags, array_values($missing));
                    $changed = true;
                }

                $wanted = (string)$instance->{'auth-gen-token'} === '' ? $this->tokenDirective() : null;
                $kept = [];
                $present = false;
                foreach ($flags as $flag) {
                    if (strpos($flag, 'auth-gen-token') === 0) {
                        if ($flag !== $wanted) {
                            $changed = true;
                            continue;
                        }
                        $present = true;
                    }
                    $kept[] = $flag;
                }
                $flags = $kept;
                if ($wanted !== null && !$present) {
                    $flags[] = $wanted;
                    $changed = true;
                }

                if (!$changed) {
                    return false;
                }
                if (isset($instance->various_flags)) {
                    $instance->various_flags = implode(',', $flags);
                } else {
                    $instance->addChild('various_flags', implode(',', $flags));
                }
                $cfg->save(['description' => sprintf(
                    'openvpn-auth-oauth2 restored the SSO directives on OpenVPN instance %s',
                    $uuid
                )]);
                return true;
            }

            return null;
        } finally {
            // save() releases the file lock but leaves the Config marked as
            // locked, so unlock() is needed on every path
            $cfg->unlock();
        }
    }

    /**
     * Note on 'management-client-auth': see ensureClientAuthFlag(). It is
     * deliberately NOT validated here, because messages appended in
     * performValidation are hard errors that block the save, which would make
     * the plugin impossible to enable whenever the flag is absent. Enforcement
     * happens at runtime instead: the SSO guard in supervisor.py stops the
     * instance whenever it runs without the directive, and the status panel
     * reports it.
     */
    public function performValidation($validateFullModel = false)
    {
        $messages = parent::performValidation($validateFullModel);

        $enabled = (string)$this->general->enabled === '1';
        $instanceRef = (string)$this->general->vpnInstance;

        if ($enabled && $instanceRef === '') {
            $messages->appendMessage(
                new Message(gettext('An OpenVPN server instance must be selected when the service is enabled.'), 'general.vpnInstance')
            );
        }

        if ($enabled && (string)$this->entra->tenantId === '' && (string)$this->entra->issuer === '') {
            $messages->appendMessage(
                new Message(gettext('A tenant ID (or a custom issuer URL) is required when the service is enabled.'), 'entra.tenantId')
            );
        }
        if ($enabled && (string)$this->entra->clientId === '') {
            $messages->appendMessage(
                new Message(gettext('A client ID is required when the service is enabled.'), 'entra.clientId')
            );
        }
        if ($enabled && (string)$this->http->baseUrl === '') {
            $messages->appendMessage(
                new Message(gettext('A public base URL is required when the service is enabled.'), 'http.baseUrl')
            );
        }
        if ($enabled && (string)$this->http->secret === '') {
            $messages->appendMessage(
                new Message(gettext('An encryption secret (16, 24 or 32 characters) is required when the service is enabled.'), 'http.secret')
            );
        }
        $listen = (string)$this->http->listenAddress;
        if ($listen !== '' && filter_var($listen, FILTER_VALIDATE_IP) === false) {
            $messages->appendMessage(
                new Message(gettext('Listen address must be an IPv4 or IPv6 address.'), 'http.listenAddress')
            );
        }
        if ($enabled && (string)$this->http->tlsEnabled === '1' && (string)$this->http->certificate === '') {
            $messages->appendMessage(
                new Message(gettext('Select a certificate or disable TLS on the listener.'), 'http.certificate')
            );
        }
        // the Mask checks the data URI but cannot cap its length
        $logo = (string)$this->page->logo;
        $comma = strpos($logo, ',');
        if ($comma !== false && strlen(base64_decode(substr($logo, $comma + 1))) > self::LOGO_MAX_BYTES) {
            $messages->appendMessage(
                new Message(gettext('The logo must be 64 KB or smaller.'), 'page.logo')
            );
        }

        return $messages;
    }
}
