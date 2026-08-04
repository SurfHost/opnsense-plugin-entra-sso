<?php

/*
 * Copyright (c) 2026 SurfHost.nl
 * SPDX-License-Identifier: MIT
 */

namespace OPNsense\OpenVPNAuthOAuth2\Api;

use OPNsense\Base\ApiMutableModelControllerBase;

class SettingsController extends ApiMutableModelControllerBase
{
    protected static $internalModelName = 'openvpnauthoauth2';
    protected static $internalModelClass = '\OPNsense\OpenVPNAuthOAuth2\OpenVPNAuthOAuth2';
}
