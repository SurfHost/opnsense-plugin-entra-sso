#!/usr/local/bin/python3

"""
    Copyright (c) 2026 SurfHost.nl
    SPDX-License-Identifier: MIT

    Health probe for the UI status panel. Emits one JSON object describing
    supervisor, daemon, socket-swap, callback-listener and OpenVPN
    prerequisite state.
"""

import json
import os
import socket
import ssl
import stat
import subprocess
import sys
import xml.etree.ElementTree as ElementTree

SUPERVISOR_CONF = '/usr/local/etc/openvpn-auth-oauth2/supervisor.conf'
# the CHILD pidfile written by daemon(8) -p: it holds supervisor.py's pid.
# /var/run/openvpnauthoauth2.pid (-P) is the daemon(8) wrapper, which is
# alive in every failure mode and says nothing about supervisor health.
PIDFILE = '/var/run/openvpnauthoauth2.child.pid'
DAEMON_NAME = 'openvpn-auth-oauth2'
CONFIG_XML = '/conf/config.xml'
REQUIRED_FLAG = 'management-client-auth'


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


def socket_alive(path):
    if not is_socket(path):
        return False
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(2)
    try:
        probe.connect(path)
        return True
    except OSError:
        return False
    finally:
        probe.close()


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


def listener_up(address, port, tls_enabled=False):
    """Probe the callback listener. When TLS is on, complete a handshake:
    a bare connect-and-close makes the Go server log a TLS handshake error
    on every poll, and a completed handshake is the truer health signal."""
    if address in ('', '0.0.0.0', '::'):
        address = '127.0.0.1'
    try:
        with socket.create_connection((address, int(port)), timeout=1) as sock:
            if tls_enabled:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
                with context.wrap_socket(sock):
                    return True
            return True
    except (OSError, ValueError, ssl.SSLError):
        return False


def client_auth_flag(instance_uuid, config_xml=CONFIG_XML):
    """Report whether the selected OpenVPN instance carries the
    'management-client-auth' directive. Core's various_flags field is a
    closed OptionField that does not offer it, so this is advisory: without
    the flag OpenVPN never defers client connects and SSO stays silent."""
    if not instance_uuid:
        return None
    try:
        root = ElementTree.parse(config_xml).getroot()
    except (OSError, ElementTree.ParseError):
        return None
    for instance in root.iter('Instance'):
        if instance.get('uuid') != instance_uuid:
            continue
        flags = (instance.findtext('various_flags') or '').split(',')
        return REQUIRED_FLAG in [flag.strip() for flag in flags]
    return None


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
        # the GUI path holds the pass-through proxy, which expects frontend
        # connections, so probing it is safe and tells corpses from listeners
        gui = socket_alive(conf.get('gui_socket', ''))
        swapped = is_socket(conf.get('swapped_socket', ''))
        if passthrough:
            result['swap'] = 'active' if (swapped and gui and result['daemon']) else 'inactive'
        else:
            result['swap'] = 'disabled'
        result['listener'] = listener_up(
            conf.get('listen_address', ''),
            conf.get('listen_port', '0'),
            conf.get('tls_enabled') == '1',
        )
        result['client_auth_flag'] = client_auth_flag(conf.get('instance_uuid', ''))
    else:
        result['swap'] = 'disabled'
        result['listener'] = False
        result['client_auth_flag'] = None

    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    sys.exit(main())
