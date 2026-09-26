#!/usr/local/bin/python3

"""
    Copyright (c) 2026 SurfHost.nl
    SPDX-License-Identifier: MIT

    Lifecycle supervisor for openvpn-auth-oauth2 on OPNsense.

    OPNsense's OpenVPN Instances hardcode `management /var/etc/openvpn/
    instance-{uuid}.sock unix` and the GUI polls that socket for status/kill.
    (Core defines that path in OpenVPN/FieldTypes/InstanceField.php; the
    server{vpnid}.sock form is OpenVPN.php's own definition for the legacy
    pre-Instances servers. Assuming that form here is why the first release
    did nothing at all.)
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

    SSO guard (fail closed). Without 'management-client-auth' OpenVPN admits
    every client with a valid certificate on the certificate alone, and the
    instance's Options field drops the directive whenever the instance is
    saved in the GUI. The invariant: while the plugin is enabled, the
    protected instance may only run as an OpenVPN process whose loaded config
    provably contained a bare 'management-client-auth' line. Any other
    process is stopped. Nothing ever removes the directives automatically.

    * Client-auth is fixed per process: OpenVPN applies the management flags
      when it first opens the interface and core never sends an instance
      SIGHUP, so a verdict holds for one (pid, pidfile inode, pidfile mtime)
      and is persisted in /var/run so a respawned supervisor trusts it.
      The proof itself lives in enforcement.py.
    * The guard runs in its own thread with a 0.25s poll, independent of the
      blocking socket work below. A guard thread that dies makes this
      process exit, so daemon(8) -R respawns both.
    * An open instance gets SIGTERM twice, 0.3s apart: with
      explicit-exit-notify the first one only schedules the exit and the
      server keeps serving for 2s. SIGKILL is the last resort, followed by
      removing the stale pidfile and destroying a DCO interface, whose kernel
      socket reference would otherwise outlive the process. A process that
      outlives SIGKILL keeps the instance held and is stopped again every
      5s until it is gone.
    * The guard never holds core's .stat lock across anything (one
      non-blocking shared read at a time) and never runs 'configctl openvpn
      stop' or 'configure'. After a stop it restores the directives through
      the plugin's pluginctl hook and starts the instance with core's own
      'configctl openvpn start', at most once per violation and three times
      per 15 minutes; otherwise it keeps the instance stopped (held down)
      and pauses the SSO daemon. Core's start takes the tunnel interface
      down before it looks at the pidfile, so the guard waits up to 30s
      while core holds the instance's .stat lock, does not start an
      instance that runs again by then (that counts as one of the three),
      and first removes a stale pidfile, since core trusts any live pid in
      it. A repair that a restart of this process interrupted is taken up
      again at startup.
"""

import base64
import collections
import glob
import os
import signal
import socket
import stat
import subprocess
import sys
import syslog
import threading
import time
import traceback
import xml.etree.ElementTree as ElementTree

# root must not leave __pycache__ behind in the package's script directory
sys.dont_write_bytecode = True
import enforcement  # noqa: E402  (sibling module, sys.path[0] is this directory)

ETC_DIR = '/usr/local/etc/openvpn-auth-oauth2'
SUPERVISOR_CONF = ETC_DIR + '/supervisor.conf'
DAEMON_BIN = '/usr/local/sbin/openvpn-auth-oauth2'
DAEMON_NAME = 'openvpn-auth-oauth2'
GUI_DIR = '/var/etc/openvpn'
SWAP_DIR = '/var/etc/openvpn-auth-oauth2'
CONFIG_XML = '/conf/config.xml'
SOCKET_WAIT_TIMEOUT = 60
PROXY_WAIT_TIMEOUT = 15
# how long an empty GUI path is attributed to a proxy that has not bound yet
# rather than to an OpenVPN restart; measured from daemon start, so it
# subsumes PROXY_WAIT_TIMEOUT
PROXY_GRACE = 120
CONFIG_RETRY_DELAY = 30
RAPID_EXIT_WINDOW = 10
MAX_BACKOFF = 60
STUCK_CYCLES = 3

# SSO guard timings (seconds)
TICK = 0.25
CONFIG_DEBOUNCE = 0.5
CONFIG_REPAIR_MIN_INTERVAL = 10
# core's waitforpid() allows a start 10s before it writes the start record
UNVERIFIED_GRACE = 20
TERM_SECOND_AFTER = 0.3
TERM_GRACE = 5
KILL_GRACE = 2
# how often a process that outlived its stop is stopped again, and how often
# a pid found not to be this instance's OpenVPN is looked at again
RESTOP_INTERVAL = 5
INSTANCE_RECHECK = 5
HELPER_TIMEOUT = 60
# below configctl's own 120s socket timeout (core configd_ctl.py), past which
# configctl exits 0 with 'error in configd communication' on stderr: the
# guard has to give up first for a stalled start to be seen as one
START_TIMEOUT = 110
# how long a repair start waits while core holds the instance's .stat lock
START_LOCK_WAIT = 30
# a repair a previous supervisor life left unfinished is taken up again
# only when that life's last heartbeat is this recent
RESUME_WINDOW = 60
REPAIR_MAX = 3
REPAIR_WINDOW = 900
HOLD_LOG_INTERVAL = 60
HEARTBEAT = 5
ERROR_LOG_INTERVAL = 60
PLUGINCTL = '/usr/local/sbin/pluginctl'
CONFIGCTL = '/usr/local/sbin/configctl'

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
    bind, which stat() alone cannot do.

    Listening sockets only (-l), matched on the LOCAL ADDRESS column alone.
    FreeBSD 15's sockstat (usr.bin/sockstat/main.c) prints a client
    connected to the listener with '??' there and '-> path' under FOREIGN
    ADDRESS, so a GUI status poll (a configctl-spawned script, hence a
    young process that sockstat lists early) must not be taken for the
    path's owner. -w is required: without it the address columns are fixed
    at 21 characters and the 67-character instance path is cut, so nothing
    would ever match. A renamed socket keeps the path it was bound to, so
    after the swap OpenVPN's listener reports the GUI path as well; the
    first match is the right one because sockstat walks kern.file, which
    lists the newest process first: the daemon is newer than the OpenVPN
    it proxies, and a restarted OpenVPN is newer than the daemon."""
    try:
        result = subprocess.run(
            ['sockstat', '-u', '-l', '-w'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.decode('utf-8', 'replace').splitlines():
        fields = line.split()
        # USER COMMAND PID FD PROTO LOCAL_ADDRESS ...
        if len(fields) >= 6 and fields[5] == path:
            try:
                return int(fields[2])
            except ValueError:
                continue
    return None


def owned_by_daemon(path):
    """True when the listener at path demonstrably belongs to our daemon,
    False when it demonstrably does not (no daemon of ours runs, or another
    process holds the listener), None when sockstat gave no answer."""
    if daemon_proc is None or daemon_proc.poll() is not None:
        return False
    pid = listener_pid(path)
    if pid is None:
        return None
    return pid == daemon_proc.pid


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
        if hasattr(os, 'fchmod'):
            # O_CREAT applies the mode to a new file only; a leftover .tmp
            # keeps whatever mode it had
            os.fchmod(fd, mode)
        os.write(fd, data)
    finally:
        os.close(fd)
    os.replace(tmp_path, path)


def secure_config(conf, target_dir=ETC_DIR):
    """configd renders the daemon YAML with the parent directory's mode minus
    the execute bits, so a 0755 directory (created by the dependency package)
    yields a world-readable 0644 file holding the Entra client secret and the
    cookie/refresh-token encryption key. Lock both down at supervisor start
    (including the idle paths) and again before every daemon start;
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


class Guard(threading.Thread):
    """SSO guard for the protected instance: stops every OpenVPN process of
    it that cannot be proven to have loaded 'management-client-auth', and
    restores the directives in config.xml when a save of the instance in the
    GUI dropped them. Independent of run(): it keeps ticking while the
    supervisor waits for sockets or for the daemon."""

    def __init__(self, conf):
        super().__init__(name='sso-guard', daemon=True)
        self.uuid = conf.get('instance_uuid', '')
        self.auto_fix = conf.get('auto_fix', '1') != '0'
        self.p = enforcement.paths(self.uuid)
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._state = 'verifying'
        self.reason = ''
        self.current = None
        # identities: (pid, pidfile inode, pidfile mtime_ns)
        self.verified = None
        self.optional_loaded = None
        self.pending = None  # (identity, first seen)
        self.alerted = None  # identity whose G4/G5 line was logged
        self.acted = None  # the identity stopped last
        self.acted_at = None
        self.unstopped = None  # an identity that outlived its stop
        self.checked = {}  # identity: (is_instance_openvpn() result, when)
        self.violation_reason = ''
        self.held_down = False
        self.hold_reason = ''
        self.hold_since = None
        self.hold_stops = 0
        self.hold_logged_at = None
        self.awaiting_proof = False
        self.start_launched = None  # when the last guard-initiated start was launched
        self.start_wait = None  # when the repair start began waiting for core
        self.resume = None  # reason of a repair a previous supervisor life left unfinished
        self.starts = collections.deque()  # monotonic times of guard-initiated starts
        self.repairs = 0
        self.stops = 0
        self.helper = None  # (Popen, purpose, started)
        self.after_helper = None
        self.cfg_sig = None
        self.cfg_due = None
        self.last_config_repair = None
        self.saved = None
        self.pre_apply_hook = None
        self.last_event = ''
        self.last_event_at = None
        self.last_heartbeat = None
        self.state_write_failed = False
        self.error_logged_at = {}  # where an internal error came from: when G16 was last logged
        self.dirty = True

    @property
    def state(self):
        with self._lock:
            return self._state

    def _set_state(self, state, reason=''):
        with self._lock:
            if (state, reason) == (self._state, self.reason):
                return
            self._state = state
            self.reason = reason
        self.dirty = True

    def blocks_daemon(self):
        """True while the instance is being stopped, repaired or kept
        stopped: starting the SSO daemon then only churns sockets."""
        return self.state in ('stopping', 'repairing', 'held_down')

    def stop(self):
        self._stop_event.set()

    def _log(self, priority, message):
        log(priority, 'SSO guard: ' + message)
        self.last_event = message
        self.last_event_at = time.time()
        self.dirty = True

    def run(self):
        if not enforcement.UUID_RE.match(self.uuid):
            log(syslog.LOG_ERR, f'SSO guard: invalid OpenVPN instance uuid {self.uuid!r}, nothing is watched')
            self._stop_event.wait()
            return
        self._log(syslog.LOG_NOTICE,
                  f'watching OpenVPN instance {self.uuid} (repair {"on" if self.auto_fix else "off"})')
        self.pre_apply_hook = enforcement.core_pre_apply_hook()
        if self.pre_apply_hook is False:
            self._log(syslog.LOG_WARNING,
                      'core no longer runs pluginctl -c crl before regenerating OpenVPN configs; '
                      'an Apply may start the instance without management-client-auth until the guard stops it')
        self._adopt()
        # cfg_sig is None, so the first tick checks config.xml (upgrade, boot, respawn)
        while not self._stop_event.wait(TICK):
            try:
                self.tick()
            except Exception:  # noqa: BLE001  (the guard must outlive its own bugs)
                self._internal_error('tick')
        self._log(syslog.LOG_WARNING, f'stopped; OpenVPN instance {self.uuid} is no longer watched')

    def _internal_error(self, where):
        """G16 for the exception being handled, at most once per
        ERROR_LOG_INTERVAL for each place it comes from."""
        now = time.monotonic()
        logged_at = self.error_logged_at.get(where)
        if logged_at is None or now - logged_at >= ERROR_LOG_INTERVAL:
            self.error_logged_at[where] = now
            log(syslog.LOG_ALERT, 'SSO guard: internal error, guard keeps running: ' + traceback.format_exc())

    def _adopt(self):
        """Trust the verdict a previous supervisor life persisted for the
        process that is still running: client-auth cannot change within a
        process. Only an identity persisted as enforced is ever stored. A
        repair that life left unfinished (it stopped the instance and died
        before the start) is taken up again by the first check, when no
        process of the instance runs by then."""
        persisted = enforcement.read_state()
        if not persisted or persisted.get('uuid') != self.uuid:
            return
        heartbeat = persisted.get('heartbeat')
        if self.auto_fix and persisted.get('state') in ('stopping', 'repairing') \
                and not persisted.get('held_down') and isinstance(heartbeat, (int, float)) \
                and abs(time.time() - heartbeat) < RESUME_WINDOW:
            self.resume = str(persisted.get('reason') or '')
        if not persisted.get('verified'):
            return
        ident = enforcement.pid_identity(self.p['pid'])
        if ident is None or list(ident) != persisted['verified']:
            return
        if not enforcement.pid_alive(ident[0]) or enforcement.is_instance_openvpn(ident[0], self.uuid) is not True:
            return
        self.verified = ident
        self.optional_loaded = persisted.get('optional_loaded')
        record = enforcement.read_stat(self.p['stat'])
        md5 = record[0].get('md5') if isinstance(record, tuple) else None
        self._log(syslog.LOG_NOTICE,
                  f'OpenVPN instance {self.uuid} pid {ident[0]} enforces SSO '
                  f'(management-client-auth loaded, start record md5 {md5 or "unknown"})')

    def tick(self):
        now = time.monotonic()
        # each phase on its own: a reap or config phase that fails on every
        # tick must not take the stop path in _check_instance() or the
        # heartbeat down with it
        for where, phase in (('reap', self._reap), ('config', self._watch_config),
                             ('instance', self._check_instance)):
            try:
                phase(now)
            except Exception:  # noqa: BLE001  (the guard must outlive its own bugs)
                self._internal_error(where)
                if where == 'instance':
                    # a guard that cannot check the instance must not look
                    # like one that does: without a fresh heartbeat the
                    # status panel reports 'not watched'
                    return
        if self.dirty or self.last_heartbeat is None or time.monotonic() - self.last_heartbeat >= HEARTBEAT:
            self._write_state()

    def _reap(self, now):
        """Finish a pluginctl/configctl helper, or kill it past its timeout,
        and start the repaired instance once core is done with it."""
        if self.helper is None:
            if self.start_wait is not None:
                self._start_when_free(now)
            return
        proc, purpose, started = self.helper
        finished = proc.poll() is not None
        if not finished:
            timeout = START_TIMEOUT if purpose == 'start' else HELPER_TIMEOUT
            if now - started < timeout:
                return
            try:
                proc.kill()
                proc.wait(timeout=5)
            except (OSError, subprocess.SubprocessError):
                pass
            log(syslog.LOG_ERR, f'SSO guard: {" ".join(proc.args)} did not finish within {timeout}s')
        self.helper = None
        if purpose == 'start' and (not finished or proc.returncode != 0):
            # the instance the guard stopped would otherwise stay down with
            # the status row on a plain 'not running' and nothing that says
            # why; the hold logs the alert with the fix, and the next
            # enforced process (Apply) clears it
            self._hold('configctl openvpn start ' + (
                'did not finish' if not finished else f'exited with status {proc.returncode}'))
            return
        if purpose != 'repair' and self.after_helper != 'repair':
            return
        self.after_helper = None
        if purpose in ('repair', 'config'):
            # both run the directives hook, so a config repair that was
            # already running serves the stopped instance as well
            if finished:
                self._after_repair()
            else:
                self._hold('could not restore management-client-auth in config.xml')
        elif not self._launch([PLUGINCTL, '-c', 'openvpnauthoauth2_directives'], 'repair'):
            self._hold('could not run pluginctl to restore management-client-auth')

    def _after_repair(self):
        if self.held_down:
            return
        saved = None
        for _attempt in range(3):
            saved = enforcement.saved_directives(self.uuid)
            if saved is not None:
                break
            time.sleep(0.2)  # config.xml in the middle of a write
        if saved is not None:
            self.saved = saved
        if saved is None:
            self._hold('could not read config.xml')
            return
        if not saved.get('instance'):
            self._hold('the instance no longer exists in config.xml')
            return
        if not (saved.get('client_auth') and saved.get('optional')):
            self._hold('could not restore management-client-auth in config.xml')
            return
        self.start_wait = time.monotonic()
        self._start_when_free(self.start_wait)

    def _start_when_free(self, now):
        """Start the repaired instance, unless it runs again by then. Core's
        start runs 'ifconfig <dev> down' before it looks at the pidfile, so
        starting a running instance would cut its tunnels: while core holds
        the instance's .stat lock (it is starting, stopping or configuring
        it) this looks again on later ticks, for up to START_LOCK_WAIT."""
        if self.held_down:
            self.start_wait = None
            return
        if now - self.start_wait < START_LOCK_WAIT and enforcement.read_stat(self.p['stat']) == 'locked':
            return
        ident = enforcement.pid_identity(self.p['pid'])
        if ident is not None and ident != self.acted and enforcement.pid_alive(ident[0]) \
                and enforcement.is_instance_openvpn(ident[0], self.uuid) is not False:
            # someone else started it meanwhile (an Apply, say): the next
            # check proves or stops this process like any other. Counted as
            # a start, so an outside restarter that keeps bringing the
            # instance back open ends in the hold
            self.start_wait = None
            self._prune_starts(now)
            self.starts.append(now)
            self._log(syslog.LOG_NOTICE,
                      f'OpenVPN instance {self.uuid} was started again (pid {ident[0]}) while the SSO directives '
                      'were being restored; checking that process instead of starting the instance')
            return
        if ident is not None and not self._drop_stale_pidfile(ident):
            return  # the pidfile changed while it was looked at: look again
        self.start_wait = None
        self._prune_starts(now)
        self.starts.append(now)
        self.repairs += 1
        self.awaiting_proof = True
        self.start_launched = now
        self._log(syslog.LOG_NOTICE,
                  f'starting OpenVPN instance {self.uuid} with the SSO directives '
                  f'(repair {len(self.starts)}/{REPAIR_MAX} in 15 minutes)')
        # core's own [start] action: regenerates the config from config.xml
        # and writes the start record the next verdict is proven against
        if not self._launch([CONFIGCTL, 'openvpn', 'start', self.uuid], 'start'):
            self._hold('could not run configctl openvpn start')

    def _drop_stale_pidfile(self, ident):
        """Core's isvalidpid() is 'pgrep -F', which any live process passes,
        so a pidfile whose pid went to another program would make core's
        start skip the instance. Removed when its pid is dead or not this
        instance's OpenVPN, and only while it still names the process just
        looked at. False when the pidfile changed meanwhile."""
        if enforcement.pid_alive(ident[0]) and enforcement.is_instance_openvpn(ident[0], self.uuid) is not False:
            return True  # no evidence that it is stale: left to core
        if enforcement.pid_identity(self.p['pid']) != ident:
            return False
        try:
            os.unlink(self.p['pid'])
        except OSError:
            pass
        return True

    def _launch(self, command, purpose):
        try:
            # stderr stays inherited: daemon(8) -S forwards it to syslog
            proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        except OSError as error:
            log(syslog.LOG_ERR, f'SSO guard: cannot run {command[0]}: {error}')
            return False
        self.helper = (proc, purpose, time.monotonic())
        return True

    def _config_sig(self):
        try:
            info = os.stat(enforcement.CONFIG_XML)
        except OSError:
            return None
        return (info.st_ino, info.st_mtime_ns, info.st_size)

    def _watch_config(self, now):
        """Restore the directives within seconds of a save that dropped them
        (the instance's Options field cannot show them), so a later
        per-instance restart never starts an open instance. Never restarts
        anything itself."""
        sig = self._config_sig()
        if sig != self.cfg_sig:
            self.cfg_sig = sig
            self.cfg_due = now + CONFIG_DEBOUNCE
            return
        if self.cfg_due is None or now < self.cfg_due:
            return
        saved = enforcement.saved_directives(self.uuid)
        if saved is None:
            self.cfg_sig = None  # caught config.xml mid-write: debounce and retry
            return
        self.saved = saved
        self.cfg_due = None
        if not saved.get('instance'):
            return
        if saved['client_auth'] and saved['optional'] and saved['token'] != 'missing':
            return
        if not self.auto_fix:
            if not saved['client_auth']:
                self._log(syslog.LOG_WARNING,
                          f'the saved configuration of OpenVPN instance {self.uuid} lacks '
                          'management-client-auth and repair is off; the next start of this instance will be stopped')
            return
        if self.helper is not None:
            self.cfg_due = now + 1  # a helper is running, look again once it is done
            return
        if self.last_config_repair is not None and now - self.last_config_repair < CONFIG_REPAIR_MIN_INTERVAL:
            self.cfg_due = self.last_config_repair + CONFIG_REPAIR_MIN_INTERVAL
            return
        self.last_config_repair = now
        self._log(syslog.LOG_NOTICE, 'restoring the SSO directives in config.xml (config.xml lost them)')
        self._launch([PLUGINCTL, '-c', 'openvpnauthoauth2_directives'], 'config')

    def _idle_state(self):
        """State while no live process of the instance exists."""
        self.pending = None
        if self.held_down:
            self._set_state('held_down', self.hold_reason)
        elif self.after_helper == 'repair' or self.start_wait is not None \
                or (self.helper is not None and self.helper[1] in ('repair', 'start')):
            self._set_state('repairing', self.violation_reason)
        else:
            self._set_state('not_running')

    def _expire_proof(self, now):
        """A guard-initiated start is proven or stopped within START_TIMEOUT
        plus UNVERIFIED_GRACE of its launch. Past that, with the start
        helper done, a violation is a new event: it gets a normal repair,
        not the hold meant for a repair that came back open."""
        # _reap() kills the start helper at START_TIMEOUT, so a start helper
        # still running here means _reap() is failing: keep the proof pending
        if not self.awaiting_proof or (self.helper is not None and self.helper[1] == 'start'):
            return
        if self.start_launched is None or now - self.start_launched >= START_TIMEOUT + UNVERIFIED_GRACE:
            self.awaiting_proof = False

    def _check_instance(self, now):
        self._expire_proof(now)
        resume, self.resume = self.resume, None  # the first check only
        ident = enforcement.pid_identity(self.p['pid'])
        self.current = ident
        if ident is None or not enforcement.pid_alive(ident[0]):
            if ident is not None and ident == self.unstopped:
                self.unstopped = None  # it exited after all; a reused pid is not ours
            self._no_process(resume)
            return
        if ident == self.verified:
            self.awaiting_proof = False
            self._set_state('enforced')
            return
        if ident == self.acted:
            if ident == self.unstopped:
                self._restop(ident, now)
            else:
                self._idle_state()  # stopped for good; the pid now belongs to something else
            return
        cached = self.checked.get(ident)
        if cached is not None and (cached[0] is True or now - cached[1] < INSTANCE_RECHECK):
            instance = cached[0]
        else:
            # a False is looked at again: a live instance taken for a stale
            # pidfile would stay unguarded
            instance = enforcement.is_instance_openvpn(ident[0], self.uuid)
            if instance is not None:
                self.checked = {ident: (instance, now)}
        if instance is False:
            self._no_process(resume)  # stale pidfile, the pid now belongs to something else
            return

        verdict, reason, info = enforcement.assess(self.uuid, ident)
        if verdict == 'enforced':
            self._on_enforced(ident, info)
        elif verdict == 'open':
            self._violation(ident, 'G4', reason)
        elif verdict == 'unverified':
            self._violation(ident, 'G5', reason)
        else:
            if self.pending is None or self.pending[0] != ident:
                self.pending = (ident, now)
                self._log(syslog.LOG_NOTICE,
                          f'waiting for core to record the start of OpenVPN instance {self.uuid} '
                          f'pid {ident[0]} ({reason})')
            if now - self.pending[1] >= UNVERIFIED_GRACE:
                self._violation(ident, 'G5', f'unproven after {UNVERIFIED_GRACE}s: {reason}')
            else:
                self._set_state('verifying', reason)

    def _no_process(self, resume):
        """No live process of the instance. A repair that a previous
        supervisor life stopped it for and did not finish is taken up again
        the normal way."""
        if resume is None:
            self._idle_state()
            return
        self.violation_reason = resume
        self._log(syslog.LOG_NOTICE,
                  f'resuming the repair of OpenVPN instance {self.uuid} that a restart of the SSO service interrupted')
        self._repair(resume)

    def _on_enforced(self, ident, info):
        self.verified = ident
        self.optional_loaded = info.get('optional')
        self.pending = None
        self.awaiting_proof = False
        self.held_down = False
        self.hold_reason = ''
        self._set_state('enforced')
        self._log(syslog.LOG_NOTICE,
                  f'OpenVPN instance {self.uuid} pid {ident[0]} enforces SSO '
                  f'(management-client-auth loaded, start record md5 {info.get("md5")})')

    def _prune_starts(self, now):
        while self.starts and now - self.starts[0] >= REPAIR_WINDOW:
            self.starts.popleft()

    def _violation(self, ident, line, reason):
        pid = ident[0]
        self.violation_reason = reason
        if self.held_down:
            # never restarted while held; an external restarter (monit, HA)
            # just gets every open process stopped, logged once a minute
            now = time.monotonic()
            quiet = self.hold_logged_at is not None and now - self.hold_logged_at < HOLD_LOG_INTERVAL
            stopped = self._stop_instance(ident, quiet=quiet)
            self._note_stop(ident, stopped)
            self._set_state('held_down', self.hold_reason)
            if not quiet:
                self.hold_logged_at = now
            if not stopped:
                return  # logged by _stop_instance; _restop() tries again
            self.hold_stops += 1
            if not quiet:
                since = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.hold_since))
                self._log(syslog.LOG_WARNING,
                          f'OpenVPN instance {self.uuid} started again without SSO enforcement and was stopped '
                          f'({self.hold_stops} times since {since})')
            return

        if ident == self.alerted:
            pass  # a stop that raised is retried; alert only once per process
        elif line == 'G4':
            self._log(syslog.LOG_ALERT,
                      f'OpenVPN instance {self.uuid} pid {pid} is running WITHOUT management-client-auth; '
                      'any valid client certificate connects without SSO. Stopping it now')
        else:
            self._log(syslog.LOG_ERR,
                      f'cannot prove OpenVPN instance {self.uuid} pid {pid} loaded management-client-auth '
                      f'({reason}); stopping it')
        self.alerted = ident
        stopped = self._stop_instance(ident)
        self._note_stop(ident, stopped)
        if not stopped:
            # a start would find it still running; keep trying to stop it
            self._hold(f'pid {pid} outlived SIGKILL and is stopped again every {RESTOP_INTERVAL}s')
            return
        self._repair(reason)

    def _repair(self, reason):
        """The instance is stopped: restore the directives and start it
        again, or keep it stopped."""
        self._prune_starts(time.monotonic())
        if not self.auto_fix:
            self._hold('Repair OpenVPN instance directives is off')
        elif self.awaiting_proof:
            self._hold('it came back without management-client-auth after a repair')
        elif len(self.starts) >= REPAIR_MAX:
            self._hold(f'{REPAIR_MAX} repairs in 15 minutes')
        else:
            self._set_state('repairing', reason)
            if self.helper is not None and self.helper[1] == 'repair':
                return  # a repair is already on its way to a start
            self._log(syslog.LOG_NOTICE,
                      'restoring the SSO directives in config.xml (before restarting the stopped instance)')
            if self.helper is not None:
                self.after_helper = 'repair'  # the running config repair serves this stop
            elif not self._launch([PLUGINCTL, '-c', 'openvpnauthoauth2_directives'], 'repair'):
                self._hold('could not run pluginctl to restore management-client-auth')

    def _note_stop(self, ident, stopped):
        # only once the stop was attempted: a stop that raised leaves the
        # process unacted and its pending timer running, so the next check
        # stops it again
        self.pending = None
        self.acted = ident
        self.acted_at = time.monotonic()
        if stopped:
            self.stops += 1
            self.unstopped = None
        else:
            self.unstopped = ident

    def _restop(self, ident, now):
        """Stop again, every RESTOP_INTERVAL, a process that outlived its
        stop. The instance is held by then, so nothing starts it again. ps is
        asked afresh: once the pid belongs to another process, ours is gone."""
        if self.acted_at is not None and now - self.acted_at < RESTOP_INTERVAL:
            return
        if enforcement.is_instance_openvpn(ident[0], self.uuid) is False:
            self.unstopped = None
            self._idle_state()
            return
        quiet = self.hold_logged_at is not None and now - self.hold_logged_at < HOLD_LOG_INTERVAL
        if not quiet:
            self.hold_logged_at = now
        self._note_stop(ident, self._stop_instance(ident, quiet=quiet))
        self._set_state('held_down', self.hold_reason)

    def _hold(self, reason):
        """Keep the instance stopped: a process that is not enforced is
        stopped and never restarted, until the next enforced verdict (Apply
        on VPN > OpenVPN > Instances starts it) or a supervisor restart
        clears the hold. Save on the SSO page usually does not start it: the
        config watcher has put the directives back in config.xml by then, so
        the page finds nothing to repair."""
        self.held_down = True
        self.hold_reason = reason
        self.hold_since = time.time()
        self.hold_stops = 0
        self.hold_logged_at = None
        self.after_helper = None
        self.start_wait = None
        self._set_state('held_down', reason)
        if self.auto_fix:
            fix = 'Press Apply on VPN > OpenVPN > Instances to restore the directives and start it'
        else:
            fix = 'Turn on Repair OpenVPN instance directives, or restore the directives and press Apply'
        self._log(syslog.LOG_ALERT, f'keeping OpenVPN instance {self.uuid} stopped: {reason}. {fix}')

    @staticmethod
    def _signal(pid, signum):
        try:
            os.kill(pid, signum)
        except OSError:
            pass

    def _stop_instance(self, ident, quiet=False):
        """Stop the process directly, never through core: 'configctl openvpn
        stop' would wait for core's .stat lock, which a concurrent start holds
        for up to 10s while the open process serves clients. True once the
        process is gone."""
        pid = ident[0]
        self._set_state('stopping', self.violation_reason)
        self._write_state()  # visible to the status panel while this blocks
        started = time.monotonic()
        self._signal(pid, signal.SIGTERM)
        time.sleep(TERM_SECOND_AFTER)
        if enforcement.pid_alive(pid):
            # with explicit-exit-notify the first SIGTERM only schedules the
            # exit and the server keeps serving for 2s; the second ends it
            self._signal(pid, signal.SIGTERM)
        deadline = started + TERM_GRACE
        while enforcement.pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        alive = enforcement.pid_alive(pid)
        if alive and enforcement.is_instance_openvpn(pid, self.uuid) is False:
            alive = False  # the pid already belongs to another process, ours is gone
        if alive:
            # the pidfile identity tied this pid to the instance; a failing
            # ps must not spare an open instance
            self._signal(pid, signal.SIGKILL)
            deadline = time.monotonic() + KILL_GRACE
            while enforcement.pid_alive(pid) and time.monotonic() < deadline:
                time.sleep(0.1)
            if not quiet:
                self._log(syslog.LOG_ERR, f'pid {pid} ignored SIGTERM for {TERM_GRACE}s; sent SIGKILL')
            alive = enforcement.pid_alive(pid)
            if not alive:
                # a killed OpenVPN leaves its pidfile, and core's isvalidpid()
                # and killbypid() trust whatever pid it holds
                if enforcement.pid_identity(self.p['pid']) == ident:
                    try:
                        os.unlink(self.p['pid'])
                    except OSError:
                        pass
                self._destroy_dco()
        if alive:
            if not quiet:
                self._log(syslog.LOG_ERR,
                          f'could not stop OpenVPN instance {self.uuid} pid {pid}: it outlived SIGKILL; '
                          f'trying again every {RESTOP_INTERVAL}s')
            return False
        if not quiet:
            self._log(syslog.LOG_NOTICE,
                      f'stopped OpenVPN instance {self.uuid} pid {pid} after '
                      f'{time.monotonic() - started:.1f}s; every session it admitted was dropped')
        return True

    def _destroy_dco(self):
        """if_ovpn keeps its own reference on the UDP socket until the
        interface is destroyed, so DCO peers can outlive a killed process.
        Core recreates the interface on the next start."""
        record = enforcement.read_stat(self.p['stat'])
        if not isinstance(record, tuple):
            return
        data = record[0]
        devname = str(data.get('devname') or '')
        if data.get('dev_type') != 'ovpn' or not (
                devname[:4] == 'ovpn' and devname[4:5].isalpha() and devname[5:].isdigit()):
            return
        try:
            result = subprocess.run(
                ['/sbin/ifconfig', devname, 'destroy'], stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=10, check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            log(syslog.LOG_WARNING, f'SSO guard: cannot destroy DCO interface {devname}: {error}')
            return
        if result.returncode == 0:
            self._log(syslog.LOG_NOTICE,
                      f'destroyed DCO interface {devname} so no kernel peer outlives the stopped process')

    def _write_state(self):
        with self._lock:
            state, reason = self._state, self.reason
        ident = self.current
        written = enforcement.write_state({
            'version': 1,
            'uuid': self.uuid,
            'supervisor_pid': os.getpid(),
            'heartbeat': time.time(),
            'state': state,
            'reason': reason,
            'pid': ident[0] if ident else None,
            'identity': list(ident) if ident else None,
            'verified': list(self.verified) if self.verified else None,
            'optional_loaded': self.optional_loaded,
            'stops': self.stops,
            'repairs': self.repairs,
            'held_down': self.held_down,
            'auto_fix': self.auto_fix,
            'pre_apply_hook': self.pre_apply_hook,
            'saved': self.saved,
            'last_event': self.last_event,
            'last_event_at': self.last_event_at,
        })
        if not written and not self.state_write_failed:
            log(syslog.LOG_WARNING, f'SSO guard: cannot write {enforcement.STATE_FILE}')
        self.state_write_failed = not written
        self.last_heartbeat = time.monotonic()
        self.dirty = False


def handle_signal(signum, frame):
    global shutting_down
    shutting_down = True


def run(conf, guard):
    gui_socket = conf['gui_socket']
    swapped_socket = conf['swapped_socket']
    passthrough = conf.get('passthrough') == '1'
    uuid = conf.get('instance_uuid', '')

    # reconciliation: a previous supervisor may have died at any point
    kill_orphan_daemons()

    proxy_ident = None
    backoff = 2
    stuck = 0
    paused = False

    def check_guard():
        if not guard.is_alive():
            log(syslog.LOG_ERR, 'SSO guard thread stopped unexpectedly; restarting the supervisor')
            raise RuntimeError('SSO guard stopped')

    def pause_for_guard():
        nonlocal paused
        if not paused:
            log(syslog.LOG_NOTICE,
                f'OpenVPN instance {uuid} is held stopped by the SSO guard; SSO daemon paused')
            paused = True

    while not shutting_down:
        check_guard()
        if guard.blocks_daemon():
            pause_for_guard()
            sleep_interruptible(1)
            continue

        if not export_tls_material(conf):
            sleep_interruptible(CONFIG_RETRY_DELAY)
            continue

        if passthrough:
            openvpn_ident = ensure_swap(gui_socket, swapped_socket)
            proxy_ident = None
        else:
            openvpn_ident = wait_for_live_socket(gui_socket)
        if openvpn_ident is None:
            if not shutting_down and guard.state == 'held_down':
                pause_for_guard()  # no socket is expected while the guard keeps it stopped
                sleep_interruptible(1)
            elif not shutting_down:
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

        # the guard may have started stopping or repairing the instance
        # while ensure_swap() waited for its socket
        if guard.blocks_daemon():
            pause_for_guard()
            sleep_interruptible(1)
            continue
        paused = False

        if not start_daemon(conf):
            sleep_interruptible(CONFIG_RETRY_DELAY)
            continue
        started = time.monotonic()

        if passthrough:
            proxy_ident = wait_for_proxy(gui_socket, swapped_socket, openvpn_ident)
            if proxy_ident is None and not shutting_down \
                    and daemon_proc is not None and daemon_proc.poll() is None:
                log(syslog.LOG_WARNING,
                    f'pass-through proxy not at the GUI path after {PROXY_WAIT_TIMEOUT}s, '
                    f'still waiting (up to {PROXY_GRACE}s from daemon start)')

        while not shutting_down:
            time.sleep(1)
            check_guard()
            if daemon_proc is not None and daemon_proc.poll() is not None:
                log(syslog.LOG_ERR, 'openvpn-auth-oauth2 exited unexpectedly, restarting cycle')
                break
            if passthrough:
                if socket_ident(swapped_socket) != openvpn_ident:
                    log(syslog.LOG_NOTICE, 'swapped management socket disturbed, restarting cycle')
                    break
                current = socket_ident(gui_socket)
                if current is None:
                    if proxy_ident is None:
                        # our proxy has not bound yet: an empty GUI path here is
                        # not evidence of an OpenVPN restart
                        if time.monotonic() - started < PROXY_GRACE:
                            continue
                        log(syslog.LOG_ERR,
                            'pass-through proxy never appeared at the GUI path, restarting cycle')
                        break
                    log(syslog.LOG_NOTICE, 'OpenVPN restart detected, re-running socket swap')
                    break
                if proxy_ident is None:
                    owned = owned_by_daemon(gui_socket)
                    if owned:
                        proxy_ident = current  # daemon bound its proxy late; adopt it
                    elif owned is False:
                        log(syslog.LOG_NOTICE, 'OpenVPN re-bound the GUI path, re-running socket swap')
                        break
                    # no answer from sockstat: looked at again next second
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
            # can coincide with an OpenVPN restart. Only on positive evidence
            # that the socket is not our proxy: swapping the proxy over the
            # live OpenVPN socket would unlink the latter, and SSO would stay
            # down until the instance is restarted. When sockstat gives no
            # answer the swap is left to the next cycle, which finds a live
            # OpenVPN socket at the GUI path anyway (SIGKILL unlinks nothing).
            current = socket_ident(gui_socket)
            if current is not None and current != proxy_ident \
                    and owned_by_daemon(gui_socket) is False and socket_alive(gui_socket):
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
    # Lock the config directory down unconditionally. start_daemon() is only
    # reached once a live management socket exists, so leaving this to the
    # daemon start path leaves the rendered secrets at 0644 whenever the
    # service idles (disabled, no vpnid) or waits for OpenVPN to come up.
    secure_config(conf)
    if conf.get('enabled') != '1':
        reconcile_idle()
        idle_forever('service not enabled in configuration')
        return 0
    if not conf.get('vpnid'):
        reconcile_idle()
        idle_forever('no OpenVPN instance resolved from configuration')
        return 1

    # the idle paths above start no guard: nothing is protected there, and
    # the status panel reports it
    guard = Guard(conf)
    guard.start()
    try:
        run(conf, guard)
    except Exception:  # noqa: BLE001 -- last line of defense, logged verbatim
        log(syslog.LOG_ERR, 'unhandled error: ' + traceback.format_exc())
        stop_daemon(graceful=False)
        return 1  # daemon(8) -R respawns us; startup reconciliation recovers
    finally:
        guard.stop()
        guard.join(10)
    return 0


if __name__ == '__main__':
    sys.exit(main())
