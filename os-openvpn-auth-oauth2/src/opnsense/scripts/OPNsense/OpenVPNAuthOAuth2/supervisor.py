#!/usr/local/bin/python3

"""
    Copyright (c) 2026 SurfHost.nl
    SPDX-License-Identifier: MIT

    Lifecycle supervisor for openvpn-auth-oauth2 on OPNsense.

    OPNsense's OpenVPN Instances hardcode `management /var/etc/openvpn/
    server{vpnid}.sock unix` and the GUI polls that socket for status/kill.
    OpenVPN accepts a single management client, so the SSO daemon cannot share
    the socket directly. This supervisor implements the socket swap described
    in docs/INVESTIGATION.md:

      1. wait until OpenVPN has bound the GUI socket
      2. rename it to our private path (rename keeps the bound inode alive)
      3. start openvpn-auth-oauth2: it connects to the private path and, in
         pass-through mode, re-creates a proxy socket at the original GUI path
      4. watch the GUI path; when OpenVPN restarts it re-binds the original
         path with a fresh inode — tear down, re-swap, restart the daemon

    DRAFT status: functional skeleton, untested on a live firewall. The TLS
    certificate export from the OPNsense trust store is still a stub.
"""

import os
import signal
import stat
import subprocess
import sys
import syslog
import time

SUPERVISOR_CONF = '/usr/local/etc/openvpn-auth-oauth2/supervisor.conf'
DAEMON_BIN = '/usr/local/sbin/openvpn-auth-oauth2'
RUN_DIR = '/var/run/openvpn-auth-oauth2'
POLL_INTERVAL = 5
SOCKET_WAIT_TIMEOUT = 60

daemon_proc = None
shutting_down = False


def log(priority, message):
    syslog.syslog(priority, message)


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


def socket_inode(path):
    try:
        result = os.stat(path)
        if stat.S_ISSOCK(result.st_mode):
            return result.st_ino
    except FileNotFoundError:
        pass
    return None


def wait_for_socket(path, timeout=SOCKET_WAIT_TIMEOUT):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not shutting_down:
        inode = socket_inode(path)
        if inode is not None:
            return inode
        time.sleep(1)
    return None


def export_tls_material(conf):
    """Export the selected trust-store certificate to http.crt/http.key.

    TODO: implement via the config.xml trust store (base64 crt/prv of the
    refid in conf['certificate_ref']), same pattern os-caddy uses. Until
    then the operator can place the files manually; missing files only
    break TLS startup, which the daemon reports clearly.
    """
    if conf.get('tls_enabled') != '1':
        return
    for name in ('http.crt', 'http.key'):
        target = os.path.join('/usr/local/etc/openvpn-auth-oauth2', name)
        if not os.path.exists(target):
            log(syslog.LOG_WARNING, f'TLS enabled but {target} is missing (cert export not implemented yet)')


def swap_socket(gui_socket, swapped_socket):
    """Move OpenVPN's bound management socket out of the GUI path."""
    os.makedirs(RUN_DIR, mode=0o750, exist_ok=True)
    if os.path.exists(swapped_socket):
        os.unlink(swapped_socket)
    os.rename(gui_socket, swapped_socket)
    log(syslog.LOG_NOTICE, f'management socket swapped: {gui_socket} -> {swapped_socket}')


def start_daemon(conf):
    global daemon_proc
    export_tls_material(conf)
    daemon_proc = subprocess.Popen(
        [DAEMON_BIN, '--config', conf['daemon_config']],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    log(syslog.LOG_NOTICE, f'openvpn-auth-oauth2 started (pid {daemon_proc.pid})')


def stop_daemon():
    global daemon_proc
    if daemon_proc is not None and daemon_proc.poll() is None:
        daemon_proc.terminate()
        try:
            daemon_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            daemon_proc.kill()
    daemon_proc = None


def handle_signal(signum, frame):
    global shutting_down
    shutting_down = True


def main():
    syslog.openlog('openvpn-auth-oauth2', 0, syslog.LOG_DAEMON)
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    conf = read_conf()
    if conf.get('enabled') != '1':
        log(syslog.LOG_NOTICE, 'service not enabled in configuration, exiting')
        return 0
    if not conf.get('vpnid'):
        log(syslog.LOG_ERR, 'no OpenVPN instance resolved from configuration, exiting')
        return 1

    gui_socket = conf['gui_socket']
    swapped_socket = conf['swapped_socket']
    passthrough = conf.get('passthrough') == '1'

    while not shutting_down:
        inode = wait_for_socket(gui_socket)
        if inode is None:
            if not shutting_down:
                log(syslog.LOG_ERR, f'OpenVPN management socket {gui_socket} did not appear, retrying')
            continue

        if passthrough:
            swap_socket(gui_socket, swapped_socket)
        # in exclusive mode the daemon connects to the GUI path directly and
        # the rendered YAML omits the pass-through section

        start_daemon(conf)

        # watch for an OpenVPN restart: the GUI path either disappears with
        # the pass-through proxy or comes back with a different inode
        while not shutting_down:
            time.sleep(POLL_INTERVAL)
            if daemon_proc is not None and daemon_proc.poll() is not None:
                log(syslog.LOG_ERR, 'openvpn-auth-oauth2 exited unexpectedly, restarting cycle')
                break
            current = socket_inode(gui_socket)
            if passthrough:
                # OpenVPN re-binding the GUI path clobbers the proxy socket
                # with a fresh OpenVPN-owned inode; detect and re-swap
                if current is not None and socket_inode(swapped_socket) is None:
                    log(syslog.LOG_NOTICE, 'OpenVPN restart detected, re-running socket swap')
                    break
            elif current is None:
                log(syslog.LOG_NOTICE, 'OpenVPN management socket gone, waiting for restart')
                break

        stop_daemon()

    stop_daemon()
    log(syslog.LOG_NOTICE, 'supervisor shut down')
    return 0


if __name__ == '__main__':
    sys.exit(main())
