#!/usr/bin/env python3
"""Offline publication guard tests with synthetic data and isolated Git repos."""
import contextlib
import hashlib
import importlib.util
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('public_guard', Path(__file__).with_name('check_public_tree.py'))
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class PublicationGuardTests(unittest.TestCase):
    def test_private_paths_binary_and_credential_shapes(self):
        self.assertIn('private-file', guard.findings('private/access/config.json', b'{}'))
        self.assertIn('binary-or-key', guard.findings('firmware/image.bin', b'image'))
        self.assertIn('private-key', guard.findings('oops.txt', b'-----BEGIN ' + b'OPENSSH PRIVATE KEY-----'))
        self.assertIn('jwt', guard.findings('oops.txt', b'eyJ' + b'A' * 20 + b'.' + b'B' * 20 + b'.' + b'C' * 20))
        self.assertIn('github-token', guard.findings('oops.txt', b'ghp_' + b'a' * 36))
        self.assertIn('personal-home-path', guard.findings('docs.txt', b'/' + b'Users' + b'/someone/project/'))
        self.assertFalse(guard.findings('README.md', b'Firmware SHA256: ' + b'0' * 64))

    def test_force_added_private_file_is_not_hidden_by_gitignore(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            (root / '.gitignore').write_text('private/\n')
            (root / 'private').mkdir()
            (root / 'private/access.json').write_text('{}')
            subprocess.run(['git', '-C', str(root), 'add', '-f', 'private/access.json'], check=True)
            with patch.object(guard, 'ROOT', root), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(guard.main(), 1)

    def test_pgp_private_key_is_refused_but_public_contact_key_is_allowed(self):
        for prefix in (b'', b'OPENSSH ', b'RSA ', b'EC ', b'DSA ', b'ENCRYPTED '):
            self.assertIn('private-key', guard.findings('contact.asc', b'-----BEGIN ' + prefix + b'PRIVATE KEY-----'))
        self.assertIn('private-key', guard.findings('contact.asc', b'-----BEGIN ' + b'PGP PRIVATE KEY BLOCK-----'))
        self.assertFalse(guard.findings('contact.asc', b'-----BEGIN ' + b'PGP PUBLIC KEY BLOCK-----'))

    def test_external_and_missing_symlinks_are_refused(self):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as outside:
            root = Path(folder).resolve()
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            destination = Path(outside) / 'file.txt'
            destination.write_text('outside')
            (root / 'external.txt').symlink_to(destination)
            (root / 'missing.txt').symlink_to('missing-target.txt')
            with patch.object(guard, 'ROOT', root), contextlib.redirect_stderr(io.StringIO()) as output:
                self.assertEqual(guard.main(), 1)
                self.assertIn('external.txt: symlink-escape-or-missing', output.getvalue())
                self.assertIn('missing.txt: symlink-escape-or-missing', output.getvalue())

    def test_internal_public_symlink_is_allowed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            (root / 'header.h').write_text('#pragma once\n')
            (root / 'alias.h').symlink_to('header.h')
            with patch.object(guard, 'ROOT', root), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(guard.main(), 0)


    def test_only_exact_reviewed_photo_bytes_and_path_bypass_binary_rule(self):
        name = 'docs/images/reviewed.jpg'
        data = b'\xff\xd8\0synthetic-photo\xff\xd9'
        with patch.dict(guard.PUBLIC_PHOTOS, {name: hashlib.sha256(data).hexdigest()}, clear=True):
            self.assertFalse(guard.findings(name, data))
            self.assertIn('unreviewed-photo', guard.findings(name, data + b'changed'))
            self.assertIn('binary-content', guard.findings(name, data + b'changed'))
            self.assertIn('binary-content', guard.findings('docs/images/other.jpg', data))
            self.assertIn('binary-content', guard.findings('unexpected.jpg', data))
            self.assertIn('private-file', guard.findings('private/reviewed.jpg', data))

    def test_photo_exception_does_not_skip_credential_or_size_checks(self):
        name = 'docs/images/reviewed.jpg'
        data = b'\0ghp_' + b'a' * 36 + b' ' * (1024 * 1024)
        with patch.dict(guard.PUBLIC_PHOTOS, {name: hashlib.sha256(data).hexdigest()}, clear=True):
            issues = guard.findings(name, data)
            self.assertIn('github-token', issues)
            self.assertIn('oversized-public-file', issues)


if __name__ == '__main__':
    unittest.main()
