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
      with connect probes. A bare inode number can be reused (tmpfs /var), and
      a socket file's existence says nothing about the process behind it.
      A socket is declared dead only after two failed probes: OpenVPN binds
      the management socket before it services accept(), so a single
      ECONNREFUSED is not proof of death.
    * Ownership of the GUI path is resolved with sockstat(1) when a decision
      would otherwise be ambiguous: the daemon's pass-through proxy and a
      freshly bound OpenVPN socket are indistinguishable by stat alone.
    * Liveness probes of the SWAPPED path only run while our daemon is
      stopped: the management interface accepts a single client. The GUI path
      may be probed freely, since the pass-through proxy expects frontends.
    * Go's unix listener unlinks its socket PATH on graceful close, so the
      daemon gets SIGKILL in pass-through mode whenever a foreign socket
      could be sitting at that path. SIGTERM is used only in exclusive mode.
    * This process can die at any point (daemon(8) -R respawns it), so
      startup performs full reconciliation: kill orphaned daemons, then
      adopt or rebuild whatever swap state the filesystem shows. The idle
      paths reconcile too, so disabling the plugin cannot strand a daemon.

    TLS material for the callback listener is exported from the OPNsense
    trust store (config.xml) before each daemon start.
"""

import base64
import glob
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
GUI_DIR = '/var/etc/openvpn'
SWAP_DIR = '/var/etc/openvpn-auth-oauth2'
CONFIG_XML = '/conf/config.xml'
SOCKET_WAIT_TIMEOUT = 60
PROXY_WAIT_TIMEOUT = 15
CONFIG_RETRY_DELAY = 30
RAPID_EXIT_WINDOW = 10
MAX_BACKOFF = 60
STUCK_CYCLES = 3

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


def socket_alive(path, attempts=2, delay=0.2):
    """Probe a unix socket with short connects. Declared dead only after
    `attempts` failures: a live socket whose listen backlog is momentarily
    full answers ECONNREFUSED, which must not condemn it."""
    for attempt in range(attempts):
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(2)
        try:
            probe.connect(path)
            return True
        except OSError:
            pass
        finally:
            probe.close()
        if attempt + 1 < attempts and not shutting_down:
            time.sleep(delay)
    return False


def listener_pid(path):
    """PID owning the unix listener bound to path, or None when unknown.
    Used to tell our daemon's pass-through proxy apart from a fresh OpenVPN
    bind, which stat() alone cannot do."""
    try:
        result = subprocess.run(
            ['sockstat', '-u'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.decode('utf-8', 'replace').splitlines():
        fields = line.split()
        # USER COMMAND PID FD PROTO LOCAL_ADDRESS ...
        if len(fields) >= 6 and path in fields[5:]:
            try:
                return int(fields[2])
            except ValueError:
                continue
    return None


def owned_by_daemon(path):
    """True when the listener at path demonstrably belongs to our daemon."""
    if daemon_proc is None or daemon_proc.poll() is not None:
        return False
    pid = listener_pid(path)
    return pid is not None and pid == daemon_proc.pid


def unlink_if_same(path, expected_ident):
    """Unlink only when the file is still the one we classified as dead;
    a restarting OpenVPN may have bound a fresh socket in the meantime."""
    if expected_ident is None:
        return
    if socket_ident(path) == expected_ident:
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


def secure_config(conf, target_dir=ETC_DIR):
    """configd renders the daemon YAML with the parent directory's mode minus
    the execute bits, so a 0755 directory (created by the dependency package)
    yields a world-readable 0644 file holding the Entra client secret and the
    cookie/refresh-token encryption key. Lock both down before every start;
    with the directory at 0700 later re-renders land at 0600 by themselves."""
    try:
        os.makedirs(target_dir, mode=0o700, exist_ok=True)
        os.chmod(target_dir, 0o700)
        daemon_config = conf.get('daemon_config', '')
        if daemon_config and os.path.exists(daemon_config):
            os.chmod(daemon_config, 0o600)
    except OSError as error:
        log(syslog.LOG_WARNING, f'cannot restrict permissions on {target_dir}: {error}')


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
    # direct children only: <crl> elements embed snapshots of revoked certs
    # carrying the same refid, and iter() would happily return those instead
    for cert in root.findall('cert'):
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
        for ca in root.findall('ca'):
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
        os.makedirs(target_dir, mode=0o700, exist_ok=True)
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
    swapped_ident = socket_ident(swapped_socket)
    if swapped_ident is not None and socket_alive(swapped_socket):
        # dead proxy leftover at the GUI path; the daemon re-binds it
        unlink_if_same(gui_socket, gui_ident)
        return socket_ident(swapped_socket)

    # nothing live anywhere: clear the corpses we classified, then wait
    unlink_if_same(gui_socket, gui_ident)
    unlink_if_same(swapped_socket, swapped_ident)
    ident = wait_for_live_socket(gui_socket)
    if ident is None:
        return None
    if not swap_socket(gui_socket, swapped_socket):
        return None
    return socket_ident(swapped_socket)


def restore_socket(gui_socket, swapped_socket):
    """On clean shutdown, give the management socket back to the GUI path so
    the Connection Status page works without the pass-through proxy."""
    swapped_ident = socket_ident(swapped_socket)
    if swapped_ident is None:
        return
    if not socket_alive(swapped_socket):
        unlink_if_same(swapped_socket, swapped_ident)  # corpse, nothing to restore
        return
    gui_ident = socket_ident(gui_socket)
    if gui_ident is not None:
        if socket_alive(gui_socket):
            return  # a newer OpenVPN owns the GUI path; leave both alone
        unlink_if_same(gui_socket, gui_ident)
    try:
        if socket_ident(gui_socket) is None:
            os.replace(swapped_socket, gui_socket)
            log(syslog.LOG_NOTICE, f'management socket restored to {gui_socket}')
    except OSError as error:
        log(syslog.LOG_WARNING, f'could not restore management socket: {error}')


def reconcile_idle():
    """Recover leftovers even when parking: a previous supervisor life may
    have died uncleanly, and the freshly rendered conf (disabled, or no
    instance) carries no socket paths to work from."""
    kill_orphan_daemons()
    for swapped in glob.glob(os.path.join(SWAP_DIR, '*.sock')):
        restore_socket(os.path.join(GUI_DIR, os.path.basename(swapped)), swapped)


def start_daemon(conf):
    global daemon_proc
    secure_config(conf)
    try:
        # inherit stdout/stderr: daemon(8) -S forwards them to syslog
        daemon_proc = subprocess.Popen([DAEMON_BIN, '--config', conf['daemon_config']])
    except OSError as error:
        log(syslog.LOG_ERR, f'cannot start {DAEMON_BIN}: {error}')
        daemon_proc = None
        return False
    log(syslog.LOG_NOTICE, f'openvpn-auth-oauth2 started (pid {daemon_proc.pid})')
    return True


def stop_daemon(graceful=False):
    """Default SIGKILL: in pass-through mode a graceful Go shutdown unlinks
    whatever file sits at the proxy path, which may be a live OpenVPN socket
    by the time we tear down."""
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
    Returns its ident, or None (daemon died, swap disturbed, OpenVPN re-bound
    the path, or timeout)."""
    deadline = time.monotonic() + PROXY_WAIT_TIMEOUT
    while time.monotonic() < deadline and not shutting_down:
        if daemon_proc is None or daemon_proc.poll() is not None:
            return None
        if socket_ident(swapped_socket) != openvpn_ident:
            return None
        ident = socket_ident(gui_socket)
        if ident is not None:
            pid = listener_pid(gui_socket)
            if pid is None or pid == daemon_proc.pid:
                return ident  # ours, or sockstat unavailable: fail open
            log(syslog.LOG_NOTICE, 'OpenVPN bound the GUI path during daemon startup')
            return None
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

    proxy_ident = None
    backoff = 2
    stuck = 0

    while not shutting_down:
        if not export_tls_material(conf):
            sleep_interruptible(CONFIG_RETRY_DELAY)
            continue

        if passthrough:
            openvpn_ident = ensure_swap(gui_socket, swapped_socket)
            proxy_ident = None
        else:
            openvpn_ident = wait_for_live_socket(gui_socket)
        if openvpn_ident is None:
            if not shutting_down:
                stuck += 1
                if stuck >= STUCK_CYCLES:
                    log(syslog.LOG_ERR,
                        f'no live management socket at {gui_socket} after {stuck} attempts; '
                        'if the OpenVPN instance is running, restart it to re-create the socket')
                else:
                    log(syslog.LOG_ERR, f'no live management socket at {gui_socket}, retrying')
                sleep_interruptible(1)  # ensure_swap can fail without waiting
            continue
        stuck = 0

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
                    if owned_by_daemon(gui_socket):
                        proxy_ident = current  # daemon bound its proxy late; adopt it
                    else:
                        log(syslog.LOG_NOTICE, 'OpenVPN re-bound the GUI path, re-running socket swap')
                        break
                elif current != proxy_ident:
                    log(syslog.LOG_NOTICE, 'OpenVPN re-bound the GUI path, re-running socket swap')
                    break
            elif socket_ident(gui_socket) != openvpn_ident:
                log(syslog.LOG_NOTICE, 'OpenVPN management socket changed, restarting cycle')
                break

        if passthrough:
            # a freshly bound OpenVPN socket may sit at the GUI path; move it
            # to safety BEFORE stopping the daemon, whose teardown would
            # unlink that path. Applies to shutdown too, where a service stop
            # can coincide with an OpenVPN restart.
            current = socket_ident(gui_socket)
            if current is not None and current != proxy_ident \
                    and not owned_by_daemon(gui_socket) and socket_alive(gui_socket):
                swap_socket(gui_socket, swapped_socket)
            stop_daemon(graceful=False)
        else:
            stop_daemon(graceful=True)

        if shutting_down:
            break

        if time.monotonic() - started < RAPID_EXIT_WINDOW:
            log(syslog.LOG_WARNING, f'restarting too fast, backing off {backoff}s')
            sleep_interruptible(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)
        else:
            backoff = 2

    stop_daemon(graceful=False)
    if passthrough:
        restore_socket(gui_socket, swapped_socket)
    log(syslog.LOG_NOTICE, 'supervisor shut down')


def main():
    syslog.openlog('openvpn-auth-oauth2', 0, syslog.LOG_DAEMON)
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    conf = read_conf()
    if conf.get('enabled') != '1':
        reconcile_idle()
        idle_forever('service not enabled in configuration')
        return 0
    if not conf.get('vpnid'):
        reconcile_idle()
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
