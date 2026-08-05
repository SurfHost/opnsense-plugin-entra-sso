<?php

/*
 * Copyright (c) 2026 SurfHost.nl
 * SPDX-License-Identifier: MIT
 */

namespace OPNsense\OpenVPNAuthOAuth2;

use OPNsense\Base\BaseModel;
use OPNsense\Base\Messages\Message;

class OpenVPNAuthOAuth2 extends BaseModel
{
    /**
     * The selected OpenVPN instance must announce client connects on the
     * management interface, which requires the valueless directive
     * 'management-client-auth' in its "various flags". Core offers no hook to
     * inject it, so we can only detect and warn.
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

        if ($enabled && $instanceRef !== '') {
            $openvpn = new \OPNsense\OpenVPN\OpenVPN();
            $instance = $openvpn->getNodeByReference('Instances.Instance.' . $instanceRef);
            if ($instance !== null) {
                $flags = array_map('trim', explode(',', (string)$instance->various_flags));
                if (!in_array('management-client-auth', $flags, true)) {
                    $messages->appendMessage(
                        new Message(
                            gettext("The selected instance is missing 'management-client-auth' in its various flags; " .
                                    'SSO cannot intercept client connects without it.'),
                            'general.vpnInstance'
                        )
                    );
                }
            }
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
