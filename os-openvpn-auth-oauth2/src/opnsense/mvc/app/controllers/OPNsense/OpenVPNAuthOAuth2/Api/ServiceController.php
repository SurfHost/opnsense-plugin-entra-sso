<?php

/*
 * Copyright (c) 2026 SurfHost.nl
 * SPDX-License-Identifier: MIT
 */

namespace OPNsense\OpenVPNAuthOAuth2\Api;

use OPNsense\Base\ApiMutableServiceControllerBase;
use OPNsense\Core\Backend;

class ServiceController extends ApiMutableServiceControllerBase
{
    protected static $internalServiceClass = '\OPNsense\OpenVPNAuthOAuth2\OpenVPNAuthOAuth2';
    protected static $internalServiceTemplate = 'OPNsense/OpenVPNAuthOAuth2';
    protected static $internalServiceEnabled = 'general.enabled';
    protected static $internalServiceName = 'openvpnauthoauth2';

    /**
     * Health details for the UI status panel (supervisor, daemon, socket
     * swap, callback listener), collected by status.py via configd.
     * @return array
     */
    public function detailsAction()
    {
        // polled every 10s by the UI; never hold the session lock across configd
        $this->sessionClose();
        $backend = new Backend();
        $response = (string)$backend->configdRun('openvpnauthoauth2 details');
        $data = json_decode(trim($response), true);
        if (!is_array($data)) {
            return ['result' => 'failed'];
        }
        $data['result'] = 'ok';
        return $data;
    }
}
