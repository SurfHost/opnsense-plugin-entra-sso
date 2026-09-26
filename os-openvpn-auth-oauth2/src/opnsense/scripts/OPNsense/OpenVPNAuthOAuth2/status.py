#!/usr/local/bin/python3

"""
    Copyright (c) 2026 SurfHost.nl
    SPDX-License-Identifier: MIT

    Health probe for the UI status panel. Emits one JSON object describing
    supervisor, daemon, socket-swap, callback-listener, SSO enforcement and
    OpenVPN prerequisite state.
"""

import ipaddress
import json
import os
import socket
import ssl
import stat
import subprocess
import sys
import time
from urllib.parse import urlsplit

# root must not leave __pycache__ behind in the package's script directory
sys.dont_write_bytecode = True
import enforcement  # noqa: E402  (sibling module, sys.path[0] is this directory)

SUPERVISOR_CONF = '/usr/local/etc/openvpn-auth-oauth2/supervisor.conf'
# the CHILD pidfile written by daemon(8) -p: it holds supervisor.py's pid.
# /var/run/openvpnauthoauth2.pid (-P) is the daemon(8) wrapper, which is
# alive in every failure mode and says nothing about supervisor health.
PIDFILE = '/var/run/openvpnauthoauth2.child.pid'
DAEMON_NAME = 'openvpn-auth-oauth2'
# the guard writes its state every 5s while it runs
GUARD_FRESH = 15
# worst first; the panel shows the worse of this probe's own verdict and the
# guard's state
ENFORCEMENT_RANK = ('open', 'unverified', 'stopping', 'held_down', 'not_watched',
                    'repairing', 'verifying', 'not_running', 'enforced')


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
    on every poll, and a completed handshake is the truer health signal.
    Only an address of this firewall is probed: the settings page accepts
    any IP address, and this runs as root on every poll, so anything else
    would turn the status panel into a port probe of the far host, which a
    user with no more than this page's ACL could steer. The daemon cannot
    bind such an address anyway, so 'not listening' is the truth."""
    if address in ('', '0.0.0.0', '::'):
        address = '127.0.0.1'
    else:
        local = local_addresses()
        if local and canonical_ip(address) not in local:
            return False
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


def canonical_ip(text):
    """The address in the spelling ifconfig and getaddrinfo use, zone
    dropped; any other text unchanged, so a hostname never matches an
    address."""
    try:
        return str(ipaddress.ip_address(text.split('%', 1)[0]))
    except ValueError:
        return text


def local_addresses():
    """Every IP configured on this firewall, so the base URL and the listen
    address can be compared against them. Empty on failure, which callers
    treat as 'unknown'."""
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
            addresses.add(canonical_ip(fields[1]))
    return addresses


def base_url_status(base_url, listen_port, tls_enabled):
    """Check the public base URL against the listener it must point at. This
    cannot prove reachability from the internet, only that the URL is
    self-consistent and resolves to this firewall."""
    status = {'url': base_url, 'problems': [], 'resolves': []}
    if not base_url:
        status['problems'].append('No public base URL configured.')
        return status

    try:
        parts = urlsplit(base_url)
        host = parts.hostname
    except ValueError:
        # the model only checks the scheme; urlsplit raises on a URL such
        # as https://[::1 (unbalanced bracket), which must not take the
        # whole status probe down
        status['problems'].append('The base URL cannot be parsed.')
        return status
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


def enforcement_status(conf, saved, supervisor):
    """Whether the RUNNING OpenVPN process of the selected instance enforces
    SSO, as the worse of this probe's own verdict (enforcement.assess) and
    the guard's reported state. The saved directives in config.xml say
    nothing about the running process: OpenVPN fixes client-auth when it
    starts, so only the config it loaded counts."""
    uuid = conf.get('instance_uuid', '')
    persisted = enforcement.read_state()
    mine = persisted if persisted is not None and persisted.get('uuid') == uuid else {}
    guard = 'absent'
    fresh = False
    if persisted is not None:
        guard = 'stale'
        heartbeat = mine.get('heartbeat')
        if isinstance(heartbeat, (int, float)) and time.time() - heartbeat < GUARD_FRESH and supervisor:
            guard = 'running'
            fresh = True
    result = {
        'state': 'no_instance',
        'pid': None,
        'reason': '',
        'stops': mine.get('stops') or 0,
        'repairs': mine.get('repairs') or 0,
        'guard': guard,
        'optional_loaded': None,
    }
    if not conf.get('vpnid') or not enforcement.UUID_RE.match(uuid) or saved == {'instance': False}:
        return result

    own_reason = ''
    ident = enforcement.pid_identity(enforcement.paths(uuid)['pid'])
    if ident is None or enforcement.is_instance_openvpn(ident[0], uuid) is False:
        own = 'not_running'
    else:
        verdict, own_reason, info = enforcement.assess(uuid, ident)
        own = 'verifying' if verdict == 'pending' else verdict
        result['pid'] = ident[0]
        result['optional_loaded'] = info.get('optional')
        # the guard's persisted proof for this very process (client-auth is
        # fixed per process), valid even after the config on disk changed
        if mine.get('verified') and mine['verified'] == list(ident):
            own, own_reason = 'enforced', ''
            if mine.get('optional_loaded') is not None:
                result['optional_loaded'] = mine['optional_loaded']

    if fresh and mine.get('state') in ENFORCEMENT_RANK:
        guard_state, guard_reason = mine['state'], mine.get('reason') or ''
    else:
        guard_state, guard_reason = 'not_watched', ''
    if ENFORCEMENT_RANK.index(guard_state) < ENFORCEMENT_RANK.index(own):
        result['state'] = guard_state
        result['reason'] = guard_reason
    else:
        result['state'] = own
        result['reason'] = own_reason
    return result


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
        if passthrough:
            # the GUI path holds the pass-through proxy, which expects
            # frontend connections, so probing it is safe and tells corpses
            # from listeners
            gui = socket_alive(conf.get('gui_socket', ''))
            swapped = is_socket(conf.get('swapped_socket', ''))
            result['swap'] = 'active' if (swapped and gui and result['daemon']) else 'inactive'
        else:
            # exclusive mode: the GUI path is the real management socket with
            # the daemon as its single client, and a connect probe would
            # disturb that session, so it must not be touched here
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

        def ours(command):
            # sockstat truncates COMMAND, so match on a prefix, but demand
            # enough of one that plain 'openvpn' (or anything shorter) still
            # reads as a foreign process instead of passing as our daemon
            prefix = command.rstrip('-')
            return len(prefix) >= 8 and DAEMON_NAME.startswith(prefix)

        foreign = sorted({e['command'] for e in listeners if not ours(e['command'])})
        result['listen_conflict'] = foreign
        saved = enforcement.saved_directives(conf.get('instance_uuid', ''))
        result['saved_directives'] = saved
        result['auto_fix'] = conf.get('auto_fix', '1') != '0'
        result['pre_apply_hook'] = enforcement.core_pre_apply_hook()
        result['enforcement'] = enforcement_status(conf, saved, result['supervisor'])
        # kept for older panels: the saved security directives, token aside
        # (the instance's own Auth Token Lifetime is a valid setup too)
        if saved is not None and saved.get('instance'):
            result['client_auth_flag'] = saved['client_auth'] and saved['optional']
        else:
            result['client_auth_flag'] = None
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
        result['saved_directives'] = None
        result['auto_fix'] = None
        result['pre_apply_hook'] = None
        result['enforcement'] = {'state': 'disabled'}
        result['client_auth_flag'] = None
        result['base_url'] = {'url': '', 'problems': [], 'resolves': []}

    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    sys.exit(main())
