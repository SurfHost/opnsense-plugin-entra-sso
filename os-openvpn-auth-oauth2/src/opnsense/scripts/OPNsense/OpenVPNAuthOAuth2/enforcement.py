"""
    Copyright (c) 2026 SurfHost.nl
    SPDX-License-Identifier: MIT

    Read-only checks behind the SSO guard (supervisor.py) and the status
    panel (status.py): did the running OpenVPN process of the protected
    instance load 'management-client-auth'?

    OpenVPN fixes its management client-auth flag when it first opens the
    management interface and keeps it for the life of the process (manage.c:
    the settings are only applied while the interface is not yet defined),
    and core never sends an instance SIGHUP. So the answer is a property of
    one process, proven by the config core wrote for it and the start record
    core wrote after it started:

      * /var/etc/openvpn/instance-{uuid}.conf must hold a bare
        'management-client-auth' line (core emits every various_flags entry
        verbatim as its own line)
      * /var/etc/openvpn/instance-{uuid}.stat, which ovpn_service_control.php
        writes under an exclusive flock right after each start, must carry
        the md5 of that same config and must postdate the process's pidfile

    Core paths: OpenVPN/FieldTypes/InstanceField.php. This module never
    signals a process and never writes config.xml. It holds core's .stat lock
    only for one non-blocking read, never across anything else.
"""

import hashlib
import json
import os
import re
import subprocess
import time
import xml.etree.ElementTree as ElementTree

try:
    import fcntl
except ImportError:  # the Windows dev harness; the firewall always has it
    fcntl = None

DIRECTIVE = 'management-client-auth'
OPTIONAL = 'auth-user-pass-optional'
CONFIG_XML = '/conf/config.xml'
STATE_FILE = '/var/run/openvpnauthoauth2.guard.json'
CORE_OVPN_ACTIONS = '/usr/local/opnsense/service/conf/actions.d/actions_openvpn.conf'
UUID_RE = re.compile(r'^[0-9a-fA-F-]{36}$')
# the config of any core instance (InstanceField.php), ours or another one
INSTANCE_CONF_RE = re.compile(r'^/var/etc/openvpn/instance-[0-9a-fA-F-]{36}\.conf$')

# what OpenVPN's parser counts as whitespace (C isspace). It splits lines
# on newlines only, unlike str.splitlines().
SPACE = ' \t\r\n\v\f'
SPACE_RE = re.compile('[' + SPACE + ']+')
STABLE_TRIES = 5
STABLE_DELAY = 0.02


def paths(uuid):
    """Core's per-instance files (InstanceField.php)."""
    return {
        'conf': f'/var/etc/openvpn/instance-{uuid}.conf',
        'pid': f'/var/run/ovpn-instance-{uuid}.pid',
        'stat': f'/var/etc/openvpn/instance-{uuid}.stat',
    }


def read_stable(path):
    """The file's bytes once two consecutive reads agree, are non-empty and
    end in a newline, or None. Core writes the config in place (touch, chmod,
    write), so a single read can catch it empty or half written."""
    previous = None
    for attempt in range(STABLE_TRIES):
        try:
            with open(path, 'rb') as handle:
                data = handle.read()
        except OSError:
            return None
        if data and data.endswith(b'\n') and data == previous:
            return data
        previous = data
        if attempt + 1 < STABLE_TRIES:
            time.sleep(STABLE_DELAY)
    return None


def _inline_tag(line):
    """The closing tag when line opens an inline <tag> block the way
    OpenVPN's parser sees it (a single token, comments cut, '--' allowed),
    else None."""
    tokens = []
    for token in SPACE_RE.split(line):
        if not token:
            continue
        if token[0] in '#;':
            break
        tokens.append(token)
    if len(tokens) != 1:
        return None
    token = tokens[0]
    if len(token) >= 2 and token[0] == token[-1] and token[0] in '"\'':
        token = token[1:-1]
    if len(token) >= 3 and token.startswith('--'):
        token = token[2:]
    if len(token) > 2 and token[0] == '<' and token[-1] == '>':
        return '</' + token[1:-1] + '>'
    return None


def conf_directives(data):
    """Which SSO directives an OpenVPN config holds as bare lines. Inline
    <tag>...</tag> blocks, empty lines and comments are skipped; a line only
    counts when it is exactly the directive, optionally with the '--' prefix
    OpenVPN accepts in config files. Anything fancier reads as absent, which
    fails closed: core writes these lines bare."""
    found = {'client_auth': False, 'optional': False}
    if isinstance(data, bytes):
        # utf-8-sig drops a BOM, which OpenVPN skips as well
        data = data.decode('utf-8-sig', 'replace')
    close_tag = None
    for raw in data.split('\n'):
        line = raw.strip(SPACE)
        if close_tag is not None:
            # OpenVPN ends the block at the first line starting with the tag
            if line.startswith(close_tag):
                close_tag = None
            continue
        if not line or line[0] in '#;':
            continue
        close_tag = _inline_tag(line)
        if close_tag is not None:
            continue
        if line in (DIRECTIVE, '--' + DIRECTIVE):
            found['client_auth'] = True
        elif line in (OPTIONAL, '--' + OPTIONAL):
            found['optional'] = True
    return found


def pid_identity(pidfile):
    """(pid, inode, mtime_ns) of an OpenVPN pidfile, or None. The pidfile is
    written once per process (after daemonizing), so the triple names one
    process even when the kernel reuses its pid later."""
    try:
        with open(pidfile, 'rb') as handle:
            data = handle.read(64)
            info = os.fstat(handle.fileno())
    except OSError:
        return None
    try:
        pid = int(data.strip())
    except ValueError:
        return None
    if pid <= 1:
        return None
    return (pid, info.st_ino, info.st_mtime_ns)


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def is_instance_openvpn(pid, uuid):
    """True when pid is this instance's OpenVPN, False only on positive
    evidence that it is not (gone, another program, or started from another
    core instance's config), None when ps fails or prints something that
    does not parse. Calling a live instance a stale pidfile would leave it
    unguarded, so an OpenVPN whose config path is unknown counts as this
    instance: if core ever moves the config, the guard cannot prove the
    process and stops it."""
    try:
        # one -o per keyword: ps(1) takes everything after the first '=' of
        # an -o argument as that column's header, so 'comm=,args=' would be
        # a single comm column headed ',args='
        result = subprocess.run(
            ['ps', '-ww', '-o', 'comm=', '-o', 'args=', '-p', str(pid)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = result.stdout.decode('utf-8', 'replace').strip()
    if not output:
        # ps prints nothing and exits 1 when the pid does not exist, but also
        # when it fails on a live one (kvm_getprocs or allocation failure,
        # killed by a signal): only a pid that is really gone counts as
        # positive evidence
        if result.returncode != 0 and not pid_alive(pid):
            return False
        return None
    lines = output.splitlines()
    fields = lines[0].split(None, 1)
    if result.returncode != 0 or len(lines) != 1 or len(fields) != 2:
        return None  # a header line, or not '<comm> <args>'
    comm = fields[0]
    args = fields[1].split()
    configs = [args[i + 1] for i in range(len(args) - 1) if args[i] == '--config']
    if paths(uuid)['conf'] in configs:
        return True
    if any(INSTANCE_CONF_RE.match(config) for config in configs):
        return False  # another instance reused the pid of a stale pidfile
    return comm == 'openvpn'


def read_stat(path):
    """Core's start record: (dict, mtime_ns), 'locked' while core holds the
    exclusive lock (it is starting or stopping the instance right now), or
    None when missing or not JSON. Opened read-only so it is never created,
    locked shared and non-blocking, and released again on close. Without
    fcntl (dev harness only) the file is read unlocked."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_BINARY', 0))
    except OSError:
        return None
    try:
        if fcntl is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return 'locked'
        info = os.fstat(fd)
        chunks = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        data = json.loads(b''.join(chunks).decode('utf-8'))
    except (OSError, ValueError):
        return None
    finally:
        os.close(fd)
    if not isinstance(data, dict):
        return None
    return data, info.st_mtime_ns


def assess(uuid, ident):
    """Verdict on the process ident for instance uuid: (verdict, reason,
    info) with verdict one of 'enforced', 'open', 'unverified', 'pending'."""
    p = paths(uuid)
    conf = read_stable(p['conf'])
    if conf is None:
        return 'pending', 'config unreadable or being written', {}
    found = conf_directives(conf)
    info = {'optional': found['optional'], 'md5': hashlib.md5(conf, usedforsecurity=False).hexdigest()}
    if not found['client_auth']:
        return 'open', 'management-client-auth missing from ' + p['conf'], info
    try:
        conf_mtime = os.stat(p['conf']).st_mtime_ns
    except OSError:
        return 'pending', 'config unreadable or being written', info
    record = read_stat(p['stat'])
    if record == 'locked':
        return 'pending', 'core is still recording this start', info
    if record is None:
        return 'pending', 'no start record yet', info
    data, stat_mtime = record
    pid_mtime = ident[2]
    if stat_mtime < pid_mtime:
        return 'pending', 'start record predates this process', info
    if data.get('md5') != info['md5']:
        return 'unverified', 'config on disk differs from the one recorded at start', info
    # a second core run regenerated the config between this start and its
    # record; a later rewrite (conf newer than the record) is an unrelated
    # Apply and harmless once the md5 matches
    if pid_mtime < conf_mtime <= stat_mtime:
        return 'unverified', 'config was rewritten while this process started', info
    return 'enforced', '', info


def saved_directives(uuid, config_xml=None):
    """The directives stored on the instance in config.xml: None when the
    file cannot be parsed, {'instance': False} when the instance is gone,
    else client_auth, optional and token ('instance' when the instance's own
    Auth Token Lifetime is set, 'injected' when our auth-gen-token line is
    present, 'missing' otherwise)."""
    try:
        root = ElementTree.parse(config_xml or CONFIG_XML).getroot()
    except (OSError, ElementTree.ParseError):
        return None
    for instance in root.findall('./OPNsense/OpenVPN/Instances/Instance'):
        if instance.get('uuid') != uuid:
            continue
        flags = [flag.strip() for flag in (instance.findtext('various_flags') or '').split(',')]
        if (instance.findtext('auth-gen-token') or '').strip():
            token = 'instance'
        elif any(flag.startswith('auth-gen-token ') and flag.endswith(' external-auth') for flag in flags):
            token = 'injected'
        else:
            token = 'missing'
        return {
            'instance': True,
            'client_auth': DIRECTIVE in flags,
            'optional': OPTIONAL in flags,
            'token': token,
        }
    return {'instance': False}


def core_pre_apply_hook(path=None):
    """Whether core's [configure] action still runs 'pluginctl -c crl' before
    it regenerates the instance configs (the plugin's pre-Apply repair hangs
    off it). None when the actions file cannot be read."""
    try:
        with open(path or CORE_OVPN_ACTIONS, 'r', encoding='utf-8') as handle:
            lines = handle.read().splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    section = None
    for line in lines:
        line = line.strip()
        if line.startswith('[') and line.endswith(']'):
            section = line[1:-1].strip()
        elif section == 'configure' and line.startswith('command:'):
            return re.search(r'pluginctl\s+-c\s+crl\b', line) is not None
    return False


def read_state(path=None):
    try:
        with open(path or STATE_FILE, 'r', encoding='utf-8') as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_state(data, path=None):
    """Replace the guard state file atomically, mode 0600. Returns False when
    it cannot be written."""
    target = path or STATE_FILE
    tmp_path = target + '.tmp'
    try:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0), 0o600)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(json.dumps(data, sort_keys=True).encode('utf-8'))
        os.replace(tmp_path, target)
    except (OSError, TypeError, ValueError):
        return False
    return True
