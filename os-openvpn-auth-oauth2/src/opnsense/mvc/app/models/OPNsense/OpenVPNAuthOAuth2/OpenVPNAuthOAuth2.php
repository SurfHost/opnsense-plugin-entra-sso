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
     * @return bool|null true when something changed (caller must restart the
     *                   instance), false when everything was already in
     *                   place, null when not applicable
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

        $config = Config::getInstance()->object();
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
            Config::getInstance()->save();
            return true;
        }

        return null;
    }

    /**
     * Note on 'management-client-auth': see ensureClientAuthFlag(). It is
     * deliberately NOT validated here, because messages appended in
     * performValidation are hard errors that block the save, which would make
     * the plugin impossible to enable whenever the flag is absent. The status
     * panel reports a missing directive as a warning instead.
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

        return $messages;
    }
}
