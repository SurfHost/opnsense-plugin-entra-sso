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
     * The directive OpenVPN needs before it defers client connects to a
     * management client. Core's various_flags is a closed OptionField that
     * does not offer it, so the GUI cannot set it and silently drops it when
     * the instance is saved.
     */
    public const REQUIRED_FLAG = 'management-client-auth';

    /**
     * Add REQUIRED_FLAG to the selected instance's various_flags directly in
     * config.xml, bypassing the closed OptionField the GUI enforces. The
     * OpenVPN config generator emits various_flags entries verbatim as bare
     * directives, so the value takes effect on the next instance restart.
     *
     * Deliberately writing into core's configuration section: there is no
     * supported injection point until core accepts the directive (see
     * docs/INVESTIGATION.md). Guarded by a model toggle.
     *
     * @return bool|null true when the flag was added (caller must restart the
     *                   instance), false when already present, null when not
     *                   applicable
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
            if (in_array(self::REQUIRED_FLAG, $flags, true)) {
                return false;
            }
            $flags[] = self::REQUIRED_FLAG;
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
