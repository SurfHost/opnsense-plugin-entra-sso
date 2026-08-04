<?php

/*
 * Copyright (c) 2026 SurfHost.nl
 * SPDX-License-Identifier: MIT
 */

function openvpnauthoauth2_enabled()
{
    global $config;

    return isset($config['OPNsense']['OpenVPNAuthOAuth2']['general']['enabled']) &&
        $config['OPNsense']['OpenVPNAuthOAuth2']['general']['enabled'] == 1;
}

function openvpnauthoauth2_services()
{
    $services = [];

    if (openvpnauthoauth2_enabled()) {
        $services[] = [
            'description' => gettext('OpenVPN SSO (openvpn-auth-oauth2)'),
            'configd' => [
                'restart' => ['openvpnauthoauth2 restart'],
                'start' => ['openvpnauthoauth2 start'],
                'stop' => ['openvpnauthoauth2 stop'],
            ],
            'name' => 'openvpnauthoauth2',
            'pidfile' => '/var/run/openvpnauthoauth2.pid',
        ];
    }

    return $services;
}

function openvpnauthoauth2_syslog()
{
    $logfacilities = [];

    $logfacilities['openvpnauthoauth2'] = [
        'facility' => ['openvpn-auth-oauth2'],
    ];

    return $logfacilities;
}
