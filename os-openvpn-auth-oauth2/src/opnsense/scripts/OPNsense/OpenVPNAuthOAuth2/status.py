#!/usr/local/bin/python3

"""
    Copyright (c) 2026 SurfHost.nl
    SPDX-License-Identifier: MIT

    Health probe for the UI status panel. Emits one JSON object describing
    supervisor, daemon, socket-swap and callback-listener state.
"""

import json
import os
import socket
import stat
import subprocess
import sys

SUPERVISOR_CONF = '/usr/local/etc/openvpn-auth-oauth2/supervisor.conf'
# the CHILD pidfile written by daemon(8) -p: it holds supervisor.py's pid.
# /var/run/openvpnauthoauth2.pid (-P) is the daemon(8) wrapper, which is
# alive in every failure mode and says nothing about supervisor health.
PIDFILE = '/var/run/openvpnauthoauth2.child.pid'
DAEMON_NAME = 'openvpn-auth-oauth2'


def read_conf(path=SUPERVISOR_CONF):
    conf = {}
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            for line in handle:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    conf[key.strip()] = value.strip()
    except FileNotFoundError:
        pass
    return conf


def is_socket(path):
    try:
        return stat.S_ISSOCK(os.stat(path).st_mode)
    except (OSError, ValueError):
        return False


def supervisor_running(pidfile=PIDFILE):
    try:
        with open(pidfile, 'r', encoding='utf-8') as handle:
            pid = int(handle.read().strip())
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def daemon_running():
    try:
        return subprocess.run(
            ['pgrep', '-qx', DAEMON_NAME],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode == 0
    except OSError:
        return False


def listener_up(address, port):
    if address in ('', '0.0.0.0', '::'):
        address = '127.0.0.1'
    try:
        with socket.create_connection((address, int(port)), timeout=1):
            return True
    except (OSError, ValueError):
        return False


def main():
    conf = read_conf()
    enabled = conf.get('enabled') == '1'
    passthrough = conf.get('passthrough') == '1'

    result = {
        'enabled': enabled,
        'vpnid': conf.get('vpnid', ''),
        'supervisor': supervisor_running(),
        'daemon': daemon_running(),
    }

    if enabled:
        swapped = is_socket(conf.get('swapped_socket', ''))
        gui = is_socket(conf.get('gui_socket', ''))
        if passthrough:
            result['swap'] = 'active' if (swapped and gui) else 'inactive'
        else:
            result['swap'] = 'disabled'
        result['listener'] = listener_up(conf.get('listen_address', ''), conf.get('listen_port', '0'))
    else:
        result['swap'] = 'disabled'
        result['listener'] = False

    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    sys.exit(main())
