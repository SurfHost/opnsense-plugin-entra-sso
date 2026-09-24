<?php

/*
 * Copyright (c) 2026 SurfHost.nl
 * SPDX-License-Identifier: MIT
 */

namespace OPNsense\OpenVPNAuthOAuth2\Api;

use OPNsense\Base\ApiMutableModelControllerBase;
use OPNsense\Core\Backend;

class SettingsController extends ApiMutableModelControllerBase
{
    protected static $internalModelName = 'openvpnauthoauth2';
    protected static $internalModelClass = '\OPNsense\OpenVPNAuthOAuth2\OpenVPNAuthOAuth2';

    /**
     * Fresh encryption secret generated on the firewall (openssl rand -hex 16
     * via configd) for the UI to fill into the form; nothing is stored until
     * the user saves. Its 32 hex characters satisfy the daemon's 16/24/32
     * rule. Anything else configd hands back, such as an execute error, is
     * refused rather than put into the form. POST only, so the CSRF check
     * applies and a secret never travels in a cacheable GET response.
     * @return array
     */
    public function genSecretAction()
    {
        if (!$this->request->isPost()) {
            return ['result' => 'failed'];
        }

        $backend = new Backend();
        $secret = trim((string)$backend->configdRun('openvpnauthoauth2 gensecret'));
        if (!preg_match('/^[0-9a-f]{32}$/', $secret)) {
            return ['result' => 'failed'];
        }

        return ['result' => 'ok', 'secret' => $secret];
    }
}
