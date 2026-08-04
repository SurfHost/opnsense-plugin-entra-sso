<?php

/*
 * Copyright (c) 2026 SurfHost.nl
 * SPDX-License-Identifier: MIT
 */

namespace OPNsense\OpenVPNAuthOAuth2\Api;

use OPNsense\Base\ApiMutableServiceControllerBase;

class ServiceController extends ApiMutableServiceControllerBase
{
    protected static $internalServiceClass = '\OPNsense\OpenVPNAuthOAuth2\OpenVPNAuthOAuth2';
    protected static $internalServiceTemplate = 'OPNsense/OpenVPNAuthOAuth2';
    protected static $internalServiceEnabled = 'general.enabled';
    protected static $internalServiceName = 'openvpnauthoauth2';
}
