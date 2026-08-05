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
     * Note on 'management-client-auth': the selected OpenVPN instance must
     * announce client connects on the management interface, which requires
     * that valueless directive in its "various flags". Core's various_flags
     * is a closed OptionField that does not offer it (see
     * docs/INVESTIGATION.md), so this is deliberately NOT validated here:
     * performValidation messages are hard errors that block the save, and
     * that would make the plugin impossible to enable. The status panel
     * reports the missing directive as a warning instead.
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
