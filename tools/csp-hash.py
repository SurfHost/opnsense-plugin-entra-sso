#!/usr/bin/env python3
#
# Copyright (c) 2026 SurfHost.nl
# SPDX-License-Identifier: MIT
#
"""Keep the CSP hashes in login.gohtml in step with its inline blocks.

The page's Content-Security-Policy allows its one inline style block and its
one inline script block by sha256. A stale hash fails silently: the browser
drops the block, so the page shows unstyled or the countdown sticks at 10.

    python3 tools/csp-hash.py            rewrite the hashes after an edit
    python3 tools/csp-hash.py --check    change nothing, exit 1 when stale

Both take an optional path; the default is the plugin's login.gohtml, the
configd template. A copy that configd rendered, such as
/usr/local/etc/openvpn-auth-oauth2/login.gohtml on the firewall, works too.
The hashes must match the page as the browser gets it, after configd (jinja)
and the daemon (Go) rendered it. Both pass a block through unchanged as long
as it holds no syntax of either, which this tool checks, so the source text
of a block is what gets hashed.
"""

import base64
import hashlib
import os
import re
import sys

DEFAULT_PATH = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', 'os-openvpn-auth-oauth2', 'src',
    'opnsense', 'service', 'templates', 'OPNsense', 'OpenVPNAuthOAuth2', 'login.gohtml'))
BLOCKS = (('style', 'style-src'), ('script', 'script-src'))
GO_COMMENT = re.compile(r'\{\{-? ?/\*.*?\*/ ?-?\}\}', re.DOTALL)
# configd's jinja drops comments and statements (raw, if, set) from its
# output, and with trim_blocks the newline that follows them
JINJA_COMMENT = re.compile(r'\{#.*?#\}\n?', re.DOTALL)
JINJA_STATEMENT = re.compile(r'\{%.*?%\}\n?', re.DOTALL)


def source(body):
    digest = hashlib.sha256(body.encode('utf-8')).digest()
    return "'sha256-" + base64.b64encode(digest).decode('ascii') + "'"


def main(argv):
    check = '--check' in argv
    args = [arg for arg in argv if arg != '--check']
    path = args[0] if args else DEFAULT_PATH

    with open(path, 'rb') as handle:
        raw = handle.read()
    problems = []
    if raw.startswith(b'\xef\xbb\xbf'):
        problems.append('the file starts with a UTF-8 BOM, which breaks Content-Type sniffing')
    # browsers hash the element text after their parser turned CRLF into LF
    text = raw.decode('utf-8-sig').replace('\r\n', '\n')
    page = GO_COMMENT.sub('', text)
    output = JINJA_STATEMENT.sub('', JINJA_COMMENT.sub('', page))
    if not output.lstrip().lower().startswith('<!doctype html>'):
        problems.append('the output does not begin with <!doctype html>')

    updated = text
    for tag, directive in BLOCKS:
        bodies = re.findall(rf'<{tag}>(.*?)</{tag}>', page, re.DOTALL)
        if len(bodies) != 1:
            problems.append(f'expected one inline {tag} block, found {len(bodies)}')
            continue
        body = bodies[0]
        if '{{' in body or '{%' in body or '{#' in body:
            problems.append(f'jinja syntax or a template action inside the {tag} block')
        if '/*' in body or '<!--' in body or re.search(r'(^|[\s;{}])//', body):
            problems.append(f'comment inside the {tag} block (html/template would strip it)')
        wanted = source(body)
        pattern = re.compile(rf"({directive} )('sha256-[A-Za-z0-9+/=]*')")
        found = pattern.search(updated)
        if found is None:
            problems.append(f'the CSP has no {directive} sha256 source')
            continue
        print(f'{directive} {wanted}')
        if found.group(2) != wanted:
            if check:
                problems.append(f'{directive} lists {found.group(2)}, the {tag} block needs {wanted}')
            else:
                updated = updated[:found.start(2)] + wanted + updated[found.end(2):]

    for problem in problems:
        print(f'{path}: {problem}', file=sys.stderr)
    if problems:
        return 1
    if updated != text:
        with open(path, 'w', encoding='utf-8', newline='\n') as handle:
            handle.write(updated)
        print(f'updated {path}')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
