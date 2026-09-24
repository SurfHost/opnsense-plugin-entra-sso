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
     * swap, callback listener, OpenVPN prerequisite), collected by status.py.
     * @return array
     */
    public function detailsAction()
    {
        $backend = new Backend();
        $response = (string)$backend->configdRun('openvpnauthoauth2 details');
        $data = json_decode(trim($response), true);
        if (!is_array($data)) {
            return ['result' => 'failed'];
        }
        $data['result'] = 'ok';
        return $data;
    }

    /**
     * Repair the selected OpenVPN instance before applying our own config, so
     * the daemon comes up against an instance that actually defers client
     * connects. Runs first: restarting the instance re-creates the management
     * socket the supervisor swaps, so doing it afterwards would force an
     * immediate re-swap cycle.
     * @return array
     */
    public function reconfigureAction()
    {
        if ($this->request->isPost()) {
            $this->repairInstanceFlag();
        }

        return parent::reconfigureAction();
    }

    /**
     * Same repair on a manual service start.
     * @return array
     */
    public function startAction()
    {
        if ($this->request->isPost()) {
            $this->repairInstanceFlag();
        }

        return parent::startAction();
    }

    /**
     * Same repair on a service restart from this page.
     * @return array
     */
    public function restartAction()
    {
        if ($this->request->isPost()) {
            $this->repairInstanceFlag();
        }

        return parent::restartAction();
    }

    /**
     * Repair the SSO directives (REQUIRED_FLAGS plus the auth token
     * directive) on the selected instance and restart it so the regenerated
     * config carries them. Restarting drops the instance's active tunnels,
     * so it only happens when something was genuinely missing or stale.
     * The model takes the Config lock for the write. This is hygiene, not
     * the security boundary: the SSO guard stops an instance that runs
     * without 'management-client-auth' either way.
     */
    private function repairInstanceFlag()
    {
        $model = $this->getModel();
        if ($model->ensureClientAuthFlag() !== true) {
            return;
        }

        $uuid = (string)$model->general->vpnInstance;
        if (!preg_match('/^[0-9a-fA-F-]{36}$/', $uuid)) {
            return;
        }

        $backend = new Backend();
        $backend->configdpRun('openvpn restart', [$uuid]);
        // Config::save() logs its config event under the ident 'config' and
        // leaves it set; ours routes this line to the plugin's own log
        openlog('openvpn-auth-oauth2', LOG_ODELAY, LOG_DAEMON);
        syslog(LOG_NOTICE, sprintf(
            'repaired the SSO directives on OpenVPN instance %s and restarted it',
            $uuid
        ));
    }
}
