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

function openvpnauthoauth2_configure()
{
    return [
        /*
         * Relies on core 26.7 running 'pluginctl -c crl;' right before it
         * regenerates the instance configs on Apply, boot and CARP
         * (actions_openvpn.conf [configure]). A CRL change in System > Trust
         * runs it as well, which is harmless: only config.xml is written.
         */
        'crl' => ['openvpnauthoauth2_crl'],
        /* private hook, run by the SSO guard in supervisor.py */
        'openvpnauthoauth2_directives' => ['openvpnauthoauth2_guard_repair'],
    ];
}

function openvpnauthoauth2_crl($verbose = false)
{
    openvpnauthoauth2_restore('before OpenVPN applied its configuration');
}

function openvpnauthoauth2_guard_repair($verbose = false)
{
    openvpnauthoauth2_restore('for the SSO guard');
}

/*
 * Write the SSO directives back into the selected instance in config.xml.
 * Never calls configd and never restarts anything: on the crl path core
 * regenerates the configs right after, and a guard that stopped the
 * instance starts it itself once this hook has returned.
 */
function openvpnauthoauth2_restore($context)
{
    if (!openvpnauthoauth2_enabled()) {
        return;
    }

    $uuid = '';
    $result = null;
    $error = null;

    try {
        $mdl = new \OPNsense\OpenVPNAuthOAuth2\OpenVPNAuthOAuth2();
        $uuid = (string)$mdl->general->vpnInstance;
        $result = $mdl->ensureClientAuthFlag();
    } catch (\Throwable $e) {
        /* plugins_configure() only catches \Error */
        $error = $e->getMessage();
    }

    if ($result !== true && $error === null) {
        return;
    }

    openlog('openvpn-auth-oauth2', LOG_ODELAY, LOG_DAEMON);
    if ($error !== null) {
        syslog(LOG_ERR, sprintf('could not restore the SSO directives on OpenVPN instance %s: %s', $uuid, $error));
    } else {
        syslog(LOG_NOTICE, sprintf('restored the SSO directives on OpenVPN instance %s %s', $uuid, $context));
    }
    if (function_exists('reopenlog')) {
        /* give core's ident back to the other plugins in this pluginctl run */
        reopenlog();
    }
}
