#!/usr/local/bin/python3

"""
    Copyright (c) 2026 SurfHost.nl
    SPDX-License-Identifier: MIT

    Health probe: reports supervisor and daemon state plus whether the
    management-socket swap is currently in effect.
"""

import os
import stat
import sys


def is_socket(path):
    try:
        return stat.S_ISSOCK(os.stat(path).st_mode)
    except FileNotFoundError:
        return False


def main():
    conf = {}
    try:
        with open('/usr/local/etc/openvpn-auth-oauth2/supervisor.conf', 'r', encoding='utf-8') as handle:
            for line in handle:
                if '=' in line and not line.startswith('#'):
                    key, value = line.strip().split('=', 1)
                    conf[key] = value
    except FileNotFoundError:
        print('not configured')
        return 1

    if conf.get('enabled') != '1':
        print('disabled')
        return 1

    swapped = is_socket(conf.get('swapped_socket', ''))
    gui = is_socket(conf.get('gui_socket', ''))
    print(f"swap active: {'yes' if swapped else 'no'}; gui socket present: {'yes' if gui else 'no'}")
    return 0 if swapped and gui else 1


if __name__ == '__main__':
    sys.exit(main())
