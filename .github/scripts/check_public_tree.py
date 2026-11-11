#!/usr/bin/env python3
"""Check Git candidates without printing credential contents.

This is a small publication guard, not a complete secret scanner. Both tracked
and non-ignored untracked files are checked, including force-added private files.
"""
from pathlib import Path
import hashlib
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
PRIVATE_ROOTS = frozenset(('private', '.local', 'build'))
PRIVATE_NAMES = frozenset(('ha_ssh.json', 'secrets.yaml', '.env'))
BINARY_SUFFIXES = frozenset(('.bin', '.ota', '.elf', '.pem', '.key'))
# Only these visually reviewed, metadata-stripped photo copies are public.
# A changed image requires a fresh privacy review, not a blanket binary bypass.
PUBLIC_PHOTOS = {
    'docs/images/home-assistant.png': 'fb131375127f4af130078d23dac7b4b4d7823c53d8c4701bd7b48d1cb01305c4',
    'docs/images/controller-original.jpg': 'dd128229cb1c5d91520c82df81b2a5e1ba6a32bb09ba3b271858874f9aa16244',
    'docs/images/hlk-pm01.jpg': 'e9c1c2751632c0b8e0bfab6e1d560d90085cb1e01cd8ab31b2abedf754e16c61',
    'docs/images/xiao-prototype.jpg': '65da07a3872e08bfa3a3685f26b40b98bc6f5a846a682c6e077f1e9c480a49a7',
}
PATTERNS = {
    'private-key': re.compile(rb'-----BEGIN (?:(?:OPENSSH|RSA|EC|DSA|ENCRYPTED|PGP) )?PRIVATE KEY(?: BLOCK)?-----'),
    'jwt': re.compile(rb'(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}'),
    'github-token': re.compile(rb'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{60,})'),
    'personal-home-path': re.compile(rb'/(?:Users|home)/[A-Za-z0-9_.-]+/'),
}


def candidate_names():
    result = subprocess.run(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'],
                            cwd=ROOT, check=True, capture_output=True)
    return sorted(set(result.stdout.decode('utf-8').split('\0')) - {''})


def findings(relative, data):
    path = Path(relative)
    problems = []
    if (path.parts[0] in PRIVATE_ROOTS or path.name in PRIVATE_NAMES or
            path.name.startswith('.env.') and path.name != '.env.example'):
        problems.append('private-file')
    if path.suffix in BINARY_SUFFIXES:
        problems.append('binary-or-key')
    if len(data) > 1024 * 1024:
        problems.append('oversized-public-file')
    reviewed_photo = (relative in PUBLIC_PHOTOS and
                      hashlib.sha256(data).hexdigest() == PUBLIC_PHOTOS[relative])
    if relative in PUBLIC_PHOTOS and not reviewed_photo:
        problems.append('unreviewed-photo')
    if b'\0' in data and not reviewed_photo:
        problems.append('binary-content')
    for label, pattern in PATTERNS.items():
        if pattern.search(data):
            problems.append(label)
    return problems


def main():
    count, failures = 0, []
    for name in candidate_names():
        path = ROOT / name
        if not path.exists() and not path.is_symlink():
            continue  # A staged deletion has no current contents to publish.
        count += 1
        if path.is_symlink():
            try:
                resolved = path.resolve(strict=True)
                target = resolved.relative_to(ROOT)
            except (ValueError, FileNotFoundError, RuntimeError):
                failures.append((name, 'symlink-escape-or-missing'))
                continue
            if target.parts[0] in PRIVATE_ROOTS or not resolved.is_file():
                failures.append((name, 'symlink-to-private-or-nonfile'))
                continue
        for issue in findings(name, path.read_bytes()):
            failures.append((name, issue))
    for name, issue in failures:
        print(f'{name}: {issue}', file=sys.stderr)
    if failures:
        print(f'Publication check refused {len(failures)} finding(s); contents were not printed.', file=sys.stderr)
        return 1
    print(f'Publication check: {count} files (text or hash-reviewed photos); no configured private-file/credential/path pattern found.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
