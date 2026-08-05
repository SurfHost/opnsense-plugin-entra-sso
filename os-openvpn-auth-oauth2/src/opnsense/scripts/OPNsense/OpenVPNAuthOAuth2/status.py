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
from urllib.parse import urlsplit

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


def port_listeners(port):
    """Everything listening on this TCP port, from sockstat. A probe against
    127.0.0.1 cannot distinguish 'bound to every interface' from 'bound to
    loopback only', and cannot see that a different process owns the port."""
    entries = []
    if not str(port).isdigit():
        return entries
    try:
        result = subprocess.run(
            ['sockstat', '-l', '-P', 'tcp'], stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return entries
    for line in result.stdout.decode('utf-8', 'replace').splitlines():
        fields = line.split()
        if len(fields) < 6 or not fields[4].startswith('tcp'):
            continue
        address = fields[5]
        if address.rsplit(':', 1)[-1] == str(port):
            entries.append({'command': fields[1], 'address': address})
    return entries


def local_addresses():
    """Every IP configured on this firewall, so the base URL can be compared
    against them. Empty on failure, which callers treat as 'unknown'."""
    addresses = set()
    try:
        result = subprocess.run(
            ['ifconfig', '-a'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return addresses
    for line in result.stdout.decode('utf-8', 'replace').splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] in ('inet', 'inet6'):
            addresses.add(fields[1].split('%')[0])
    return addresses


def base_url_status(base_url, listen_port, tls_enabled):
    """Check the public base URL against the listener it must point at. This
    cannot prove reachability from the internet, only that the URL is
    self-consistent and resolves to this firewall."""
    status = {'url': base_url, 'problems': [], 'resolves': []}
    if not base_url:
        status['problems'].append('No public base URL configured.')
        return status

    parts = urlsplit(base_url)
    host = parts.hostname
    scheme = parts.scheme
    try:
        port = parts.port or (443 if scheme == 'https' else 80)
    except ValueError:
        port = None
        status['problems'].append('The base URL contains an invalid port.')
    status['host'] = host or ''
    status['port'] = port

    if scheme == 'https' and not tls_enabled:
        status['problems'].append('The base URL uses https but TLS is disabled on the listener.')
    elif scheme == 'http' and tls_enabled:
        status['problems'].append('The base URL uses http but TLS is enabled on the listener.')

    if port is not None and str(port) != str(listen_port):
        status['problems'].append(
            'The base URL port (%s) does not match the listen port (%s); '
            'the browser would be sent to the wrong port.' % (port, listen_port)
        )

    if host:
        try:
            status['resolves'] = sorted({info[4][0] for info in socket.getaddrinfo(host, None)})
        except (socket.gaierror, UnicodeError, ValueError):
            status['problems'].append('The base URL hostname does not resolve from this firewall.')
        else:
            local = local_addresses()
            if local and not set(status['resolves']) & local:
                status['problems'].append(
                    'The hostname resolves to %s, which is not an address on this firewall. '
                    'That is expected behind NAT, but check it points at this WAN.'
                    % ', '.join(status['resolves'])
                )
    return status


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
        result['listen'] = '%s:%s' % (
            conf.get('listen_address', '') or '0.0.0.0', conf.get('listen_port', ''))
        listeners = port_listeners(conf.get('listen_port', ''))
        result['listen_binds'] = [e['address'] for e in listeners]
        foreign = sorted({e['command'] for e in listeners
                          if not DAEMON_NAME.startswith(e['command'].rstrip('-'))})
        result['listen_conflict'] = foreign
        result['client_auth_flag'] = client_auth_flag(conf.get('instance_uuid', ''))
        result['base_url'] = base_url_status(
            conf.get('base_url', ''),
            conf.get('listen_port', ''),
            conf.get('tls_enabled') == '1',
        )
    else:
        result['swap'] = 'disabled'
        result['listener'] = False
        result['listen'] = ''
        result['listen_binds'] = []
        result['listen_conflict'] = []
        result['client_auth_flag'] = None
        result['base_url'] = {'url': '', 'problems': [], 'resolves': []}

    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    sys.exit(main())
