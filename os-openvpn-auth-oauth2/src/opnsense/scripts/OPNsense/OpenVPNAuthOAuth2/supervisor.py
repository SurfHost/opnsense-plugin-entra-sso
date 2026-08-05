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

      1. wait until OpenVPN has bound the GUI socket (liveness-probed, never
         trusted by mere existence)
      2. rename it to our private path (rename keeps the bound inode alive;
         both paths live under /var/etc so the rename never crosses a
         filesystem boundary)
      3. start openvpn-auth-oauth2: it connects to the private path and, in
         pass-through mode, re-creates a proxy socket at the original GUI path
      4. watch both paths; when OpenVPN restarts it re-binds the GUI path
         with a fresh socket: move that socket to safety, tear down, restart

    Hard-won invariants encoded below:

    * Sockets are identified by (st_dev, st_ino, st_birthtime) and validated
      with a short unix connect probe. A bare inode number can be reused
      (tmpfs /var), and a socket file's existence says nothing about the
      process behind it.
    * Liveness probes only run while our daemon is stopped: the management
      interface accepts a single client.
    * Go's unix listener unlinks its socket PATH on graceful close. The
      daemon therefore gets SIGKILL whenever the GUI path may hold a socket
      we care about (recycles, orphan cleanup); SIGTERM only on clean
      service stop, where the unlink is the desired cleanup.
    * This process can die at any point (daemon(8) -R respawns it), so
      startup performs full reconciliation: kill orphaned daemons, then
      adopt or rebuild whatever swap state the filesystem shows.

    TLS material for the callback listener is exported from the OPNsense
    trust store (config.xml) before each daemon start.
"""

import base64
import os
import signal
import socket
import stat
import subprocess
import sys
import syslog
import time
import traceback
import xml.etree.ElementTree as ElementTree

ETC_DIR = '/usr/local/etc/openvpn-auth-oauth2'
SUPERVISOR_CONF = ETC_DIR + '/supervisor.conf'
DAEMON_BIN = '/usr/local/sbin/openvpn-auth-oauth2'
DAEMON_NAME = 'openvpn-auth-oauth2'
SWAP_DIR = '/var/etc/openvpn-auth-oauth2'
CONFIG_XML = '/conf/config.xml'
SOCKET_WAIT_TIMEOUT = 60
PROXY_WAIT_TIMEOUT = 15
CONFIG_RETRY_DELAY = 30
RAPID_EXIT_WINDOW = 10
MAX_BACKOFF = 60

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


def sleep_interruptible(seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not shutting_down:
        time.sleep(1)


def idle_forever(message):
    """Park instead of exiting so daemon(8) -R does not respawn us in a loop."""
    log(syslog.LOG_NOTICE, message + ', idling until service stop')
    while not shutting_down:
        time.sleep(1)


def socket_ident(path):
    """Identity of the socket file at path: (dev, inode, birthtime), or None.
    birthtime guards against FreeBSD/tmpfs inode-number reuse."""
    try:
        result = os.stat(path)
        if stat.S_ISSOCK(result.st_mode):
            return (result.st_dev, result.st_ino, getattr(result, 'st_birthtime', 0))
    except OSError:
        pass
    return None


def socket_alive(path):
    """Probe a unix socket with a short connect. Only call while our daemon
    is stopped: the real management interface accepts a single client."""
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(2)
    try:
        probe.connect(path)
        return True
    except OSError:
        return False
    finally:
        probe.close()


def unlink_quiet(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def kill_orphan_daemons():
    """SIGKILL any daemon left over from a previous supervisor life. KILL,
    not TERM: a graceful Go shutdown unlinks whatever file currently sits at
    its pass-through path, which may be a live OpenVPN socket by now."""
    try:
        subprocess.run(['pkill', '-9', '-x', DAEMON_NAME], check=False)
    except OSError:
        return
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            gone = subprocess.run(['pgrep', '-qx', DAEMON_NAME], check=False).returncode != 0
        except OSError:
            return
        if gone:
            return
        time.sleep(0.2)
    log(syslog.LOG_WARNING, 'orphaned openvpn-auth-oauth2 process did not exit after SIGKILL')


def _decode_pem(node):
    if node is None or not node.text:
        return None
    try:
        pem = base64.b64decode(''.join(node.text.split()))
    except ValueError:
        return None
    # normalize so concatenated blocks stay parseable even when the
    # imported PEM lacked a trailing newline
    return pem.strip() + b'\n'


def _write_secure(path, data, mode):
    tmp_path = path + '.tmp'
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, 'O_BINARY'):  # no-op on FreeBSD, needed for the dev harness
        flags |= os.O_BINARY
    fd = os.open(tmp_path, flags, mode)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.replace(tmp_path, path)


def export_tls_material(conf, config_xml=CONFIG_XML, target_dir=ETC_DIR):
    """Export the selected trust-store certificate (plus its CA chain) to
    http.crt/http.key for the daemon's TLS listener. Returns True on success
    or when TLS is disabled."""
    if conf.get('tls_enabled') != '1':
        return True
    refid = conf.get('certificate_ref', '')
    if refid == '':
        log(syslog.LOG_ERR, 'TLS enabled but no certificate selected')
        return False

    try:
        root = ElementTree.parse(config_xml).getroot()
    except (OSError, ElementTree.ParseError) as error:
        log(syslog.LOG_ERR, f'cannot parse {config_xml}: {error}')
        return False

    cert_pem = key_pem = None
    caref = None
    for cert in root.iter('cert'):
        if cert.findtext('refid') == refid:
            cert_pem = _decode_pem(cert.find('crt'))
            key_pem = _decode_pem(cert.find('prv'))
            caref = cert.findtext('caref')
            break
    if cert_pem is None or key_pem is None:
        log(syslog.LOG_ERR, f'certificate {refid} not found in trust store or has no private key')
        return False

    # append the CA chain so clients receive intermediates
    chain = []
    depth = 0
    while caref and depth < 8:
        parent = None
        for ca in root.iter('ca'):
            if ca.findtext('refid') == caref:
                parent = ca
                break
        if parent is None:
            break
        ca_pem = _decode_pem(parent.find('crt'))
        if ca_pem:
            chain.append(ca_pem)
        caref = parent.findtext('caref')
        depth += 1

    try:
        os.makedirs(target_dir, mode=0o750, exist_ok=True)
        _write_secure(os.path.join(target_dir, 'http.crt'), b''.join([cert_pem] + chain), 0o644)
        _write_secure(os.path.join(target_dir, 'http.key'), key_pem, 0o600)
    except OSError as error:
        log(syslog.LOG_ERR, f'cannot write TLS material: {error}')
        return False
    log(syslog.LOG_NOTICE, f'TLS certificate {refid} exported from trust store')
    return True


def swap_socket(gui_socket, swapped_socket):
    """Move the bound management socket out of the GUI path. Guarded: the
    file can vanish between decision and rename when OpenVPN exits."""
    try:
        os.makedirs(SWAP_DIR, mode=0o750, exist_ok=True)
        if os.path.exists(swapped_socket):
            os.unlink(swapped_socket)
        os.replace(gui_socket, swapped_socket)
    except OSError as error:
        log(syslog.LOG_WARNING, f'socket swap failed: {error}')
        return False
    log(syslog.LOG_NOTICE, f'management socket swapped: {gui_socket} -> {swapped_socket}')
    return True


def wait_for_live_socket(path, timeout=SOCKET_WAIT_TIMEOUT):
    """Wait for a CONNECTABLE socket at path. Only call while our daemon is
    stopped. Returns its ident, or None on timeout/shutdown."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not shutting_down:
        ident = socket_ident(path)
        if ident is not None and socket_alive(path):
            return ident
        time.sleep(1)
    return None


def ensure_swap(gui_socket, swapped_socket):
    """Bring the world into the state 'live OpenVPN management socket sits at
    the swapped path'. Handles every filesystem state a crash or restart can
    leave behind. Only call while our daemon is stopped. Returns the socket's
    ident, or None to retry."""
    # a live socket at the GUI path always wins: the newest OpenVPN binds there
    gui_ident = socket_ident(gui_socket)
    if gui_ident is not None and socket_alive(gui_socket):
        if not swap_socket(gui_socket, swapped_socket):
            return None
        return socket_ident(swapped_socket)

    # otherwise adopt an intact swap from a previous supervisor life
    if socket_ident(swapped_socket) is not None and socket_alive(swapped_socket):
        if gui_ident is not None:
            unlink_quiet(gui_socket)  # dead proxy leftover; daemon re-binds it
        return socket_ident(swapped_socket)

    # nothing live anywhere: clear corpses, then wait for OpenVPN to (re)bind
    for path in (gui_socket, swapped_socket):
        if socket_ident(path) is not None:
            unlink_quiet(path)
    ident = wait_for_live_socket(gui_socket)
    if ident is None:
        if not shutting_down:
            log(syslog.LOG_ERR, f'no live management socket at {gui_socket}, retrying')
        return None
    if not swap_socket(gui_socket, swapped_socket):
        return None
    return socket_ident(swapped_socket)


def restore_socket(gui_socket, swapped_socket):
    """On clean shutdown, give the management socket back to the GUI path so
    the Connection Status page works without the pass-through proxy."""
    if socket_ident(swapped_socket) is None:
        return
    if not socket_alive(swapped_socket):
        unlink_quiet(swapped_socket)  # corpse, nothing to restore
        return
    gui_ident = socket_ident(gui_socket)
    if gui_ident is not None:
        if socket_alive(gui_socket):
            return  # a newer OpenVPN owns the GUI path; leave both alone
        unlink_quiet(gui_socket)
    try:
        os.replace(swapped_socket, gui_socket)
        log(syslog.LOG_NOTICE, f'management socket restored to {gui_socket}')
    except OSError as error:
        log(syslog.LOG_WARNING, f'could not restore management socket: {error}')


def start_daemon(conf):
    global daemon_proc
    try:
        # inherit stdout/stderr: daemon(8) -S forwards them to syslog
        daemon_proc = subprocess.Popen([DAEMON_BIN, '--config', conf['daemon_config']])
    except OSError as error:
        log(syslog.LOG_ERR, f'cannot start {DAEMON_BIN}: {error}')
        daemon_proc = None
        return False
    log(syslog.LOG_NOTICE, f'openvpn-auth-oauth2 started (pid {daemon_proc.pid})')
    return True


def stop_daemon(graceful=True):
    """graceful=False sends SIGKILL so the Go runtime cannot unlink-on-close
    the GUI path (which may hold a freshly bound OpenVPN socket)."""
    global daemon_proc
    if daemon_proc is not None and daemon_proc.poll() is None:
        try:
            if graceful:
                daemon_proc.terminate()
            else:
                daemon_proc.kill()
            try:
                daemon_proc.wait(timeout=10 if graceful else 5)
            except subprocess.TimeoutExpired:
                daemon_proc.kill()
                try:
                    daemon_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    log(syslog.LOG_ERR, f'daemon pid {daemon_proc.pid} did not die after SIGKILL')
        except OSError:
            pass
    daemon_proc = None


def wait_for_proxy(gui_socket, swapped_socket, openvpn_ident):
    """Wait for the daemon's pass-through proxy to appear at the GUI path.
    Returns its ident, or None (daemon died, swap disturbed, or timeout)."""
    deadline = time.monotonic() + PROXY_WAIT_TIMEOUT
    while time.monotonic() < deadline and not shutting_down:
        if daemon_proc is None or daemon_proc.poll() is not None:
            return None
        if socket_ident(swapped_socket) != openvpn_ident:
            return None
        ident = socket_ident(gui_socket)
        if ident is not None:
            return ident
        time.sleep(1)
    return None


def handle_signal(signum, frame):
    global shutting_down
    shutting_down = True


def run(conf):
    gui_socket = conf['gui_socket']
    swapped_socket = conf['swapped_socket']
    passthrough = conf.get('passthrough') == '1'

    # reconciliation: a previous supervisor may have died at any point
    kill_orphan_daemons()

    openvpn_ident = None
    proxy_ident = None
    backoff = 2

    while not shutting_down:
        if not export_tls_material(conf):
            sleep_interruptible(CONFIG_RETRY_DELAY)
            continue

        if passthrough:
            openvpn_ident = ensure_swap(gui_socket, swapped_socket)
            proxy_ident = None
        else:
            openvpn_ident = wait_for_live_socket(gui_socket)
            if openvpn_ident is None and not shutting_down:
                log(syslog.LOG_ERR, f'no live management socket at {gui_socket}, retrying')
        if openvpn_ident is None or shutting_down:
            continue

        if not start_daemon(conf):
            sleep_interruptible(CONFIG_RETRY_DELAY)
            continue
        started = time.monotonic()

        if passthrough:
            proxy_ident = wait_for_proxy(gui_socket, swapped_socket, openvpn_ident)
            if proxy_ident is None and not shutting_down \
                    and daemon_proc is not None and daemon_proc.poll() is None:
                log(syslog.LOG_WARNING, 'pass-through proxy slow to appear at the GUI path')

        while not shutting_down:
            time.sleep(1)
            if daemon_proc is not None and daemon_proc.poll() is not None:
                log(syslog.LOG_ERR, 'openvpn-auth-oauth2 exited unexpectedly, restarting cycle')
                break
            if passthrough:
                if socket_ident(swapped_socket) != openvpn_ident:
                    log(syslog.LOG_NOTICE, 'swapped management socket disturbed, restarting cycle')
                    break
                current = socket_ident(gui_socket)
                if current is None:
                    log(syslog.LOG_NOTICE, 'OpenVPN restart detected, re-running socket swap')
                    break
                if proxy_ident is None:
                    proxy_ident = current  # daemon bound its proxy late; adopt it
                elif current != proxy_ident:
                    log(syslog.LOG_NOTICE, 'OpenVPN re-bound the GUI path, re-running socket swap')
                    break
            else:
                current = socket_ident(gui_socket)
                if current != openvpn_ident:
                    log(syslog.LOG_NOTICE, 'OpenVPN management socket changed, restarting cycle')
                    break

        if shutting_down:
            break

        if passthrough:
            # a freshly bound OpenVPN socket may sit at the GUI path; move it
            # to safety BEFORE stopping the daemon, whose teardown could
            # unlink that path
            current = socket_ident(gui_socket)
            if current is not None and current != proxy_ident and socket_alive(gui_socket):
                if swap_socket(gui_socket, swapped_socket):
                    openvpn_ident = socket_ident(swapped_socket)
        stop_daemon(graceful=False)

        if time.monotonic() - started < RAPID_EXIT_WINDOW:
            log(syslog.LOG_WARNING, f'restarting too fast, backing off {backoff}s')
            sleep_interruptible(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)
        else:
            backoff = 2

    # clean shutdown: graceful stop lets the daemon unlink its proxy file,
    # then the real socket is renamed back for direct GUI access
    stop_daemon(graceful=True)
    if passthrough:
        restore_socket(gui_socket, swapped_socket)
    log(syslog.LOG_NOTICE, 'supervisor shut down')


def main():
    syslog.openlog('openvpn-auth-oauth2', 0, syslog.LOG_DAEMON)
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    conf = read_conf()
    if conf.get('enabled') != '1':
        idle_forever('service not enabled in configuration')
        return 0
    if not conf.get('vpnid'):
        idle_forever('no OpenVPN instance resolved from configuration')
        return 1

    try:
        run(conf)
    except Exception:  # noqa: BLE001 -- last line of defense, logged verbatim
        log(syslog.LOG_ERR, 'unhandled error: ' + traceback.format_exc())
        stop_daemon(graceful=False)
        return 1  # daemon(8) -R respawns us; startup reconciliation recovers
    return 0


if __name__ == '__main__':
    sys.exit(main())
