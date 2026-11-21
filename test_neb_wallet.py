import base64
import hashlib
import io
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import neb_wallet as wallet
from test_neb_core import OWNER, SUPPORT


NATIVE_FIXTURE = (b'# neb wallet, encrypted with a password\nneb-wallet-encrypted 1\n'
                  b'salt ' + b'a' * 32 + b'\nnonce ' + b'b' * 48 + b'\ndata ' + b'c' * 240 + b'\n')
PASSPHRASE = 'fixture-only passphrase'


def release_archive(name, body, *, link=None):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w:gz') as archive:
        member = tarfile.TarInfo(name)
        if link:
            member.type, member.linkname, body = tarfile.SYMTYPE, link, b''
        member.size = len(body)
        archive.addfile(member, io.BytesIO(body))
    return output.getvalue()


class WalletArchiveTests(unittest.TestCase):
    def test_release_digest_checked_before_tar_read(self):
        with self.assertRaisesRegex(RuntimeError, 'SHA-256'):
            wallet._verified_binary(b'not a gzip archive')

    def test_only_exact_regular_archive_member_is_accepted(self):
        binary = b'example test binary'
        for name, link in [('neb-wallet', None), ('../neb-wallet', None), ('neb-wallet', '/bin/true')]:
            with self.subTest(name=name, link=link):
                archive = release_archive(name, binary, link=link)
                with patch.object(wallet, 'ARCHIVE_SHA256', hashlib.sha256(archive).hexdigest()), \
                        patch.object(wallet, 'BINARY_SHA256', hashlib.sha256(binary).hexdigest()), \
                        patch.object(wallet, 'BINARY_SIZE', len(binary)):
                    if (name, link) == ('neb-wallet', None):
                        self.assertEqual(wallet._verified_binary(archive), binary)
                    else:
                        with self.assertRaises(RuntimeError):
                            wallet._verified_binary(archive)

    def test_unsupported_host_never_downloads(self):
        with tempfile.TemporaryDirectory() as state, patch.object(wallet, '_platform_message', return_value='glibc required'), \
                patch.object(wallet, '_download', side_effect=AssertionError('must not download')):
            store = wallet.WalletStore(Path(state))
            status = store.status()
            self.assertFalse(status['available'])
            self.assertEqual(status['items'], [])
            self.assertIn('glibc', status['message'])
            with self.assertRaisesRegex(RuntimeError, 'glibc'):
                store.install()
            self.assertEqual(list(Path(state).iterdir()), [])


@unittest.skipUnless(sys.platform == 'linux', 'Linux filesystem and PTY')
class WalletStoreTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name).resolve()
        self.store = wallet.WalletStore(self.state)
        platform = patch.object(wallet, '_platform_message', return_value=None)
        self.addCleanup(platform.stop)
        platform.start()

    def install_fixture(self):
        binary = b'placeholder; the native child is simulated by the relevant test'
        archive = release_archive('neb-wallet', binary)
        for name, value in [('ARCHIVE_SHA256', hashlib.sha256(archive).hexdigest()),
                            ('BINARY_SHA256', hashlib.sha256(binary).hexdigest()), ('BINARY_SIZE', len(binary)),
                            ('_download', lambda: archive)]:
            replacement = patch.object(wallet, name, value)
            replacement.start()
            self.addCleanup(replacement.stop)
        self.assertTrue(self.store.install()['available'])

    def child(self, binary, path, command, password):
        if password != PASSPHRASE:
            raise RuntimeError('native unlock refused')
        if command == 'new':
            path.touch(mode=0o600)
            path.write_bytes(NATIVE_FIXTURE)
            return b'not used'
        return (OWNER + '\n').encode('ascii')

    def test_exports_only_same_encrypted_wallet_after_address_check(self):
        self.install_fixture()
        with patch.object(wallet, '_run', self.child):
            created = self.store.create('Rig one', PASSPHRASE)
            self.assertEqual(created['address'], OWNER)
            self.assertEqual(self.store.status()['items'], [created])
            result = self.store.export(created['id'], PASSPHRASE)
            self.assertEqual(result['filename'], f"neb-{created['id']}.neb")
            self.assertEqual(base64.b64decode(result['backup'], validate=True), NATIVE_FIXTURE)
            with self.assertRaises(RuntimeError):
                self.store.export(created['id'], PASSPHRASE + 'wrong')
        with patch.object(wallet, '_run', return_value=(SUPPORT + '\n').encode('ascii')):
            with self.assertRaisesRegex(RuntimeError, 'адрес'):
                self.store.export(created['id'], PASSPHRASE)
        for item in self.state.rglob('*'):
            expected = 0o700 if item.is_dir() or '-v1.0.2' in item.name else 0o600
            self.assertEqual(item.stat().st_mode & 0o777, expected)
            if item.is_file():
                self.assertNotIn(PASSPHRASE.encode(), item.read_bytes())

    def test_invalid_id_and_control_character_password_are_refused(self):
        for ident in ('../wallets', '/', 'n' * 32):
            with self.subTest(ident=ident), self.assertRaises(ValueError):
                self.store.export(ident, PASSPHRASE)
        with self.assertRaises(ValueError):
            self.store.create('Rig', PASSPHRASE + '\n')
        self.assertEqual(list(self.state.iterdir()), [])

    def test_refuses_plain_and_symlinked_recovery_files(self):
        self.install_fixture()
        with patch.object(wallet, '_run', self.child):
            created = self.store.create('Rig', PASSPHRASE)
            key = self.state / 'wallets' / created['id'] / 'wallet.neb'
            key.write_bytes(b'# unencrypted fixture\n' + b'd0' * 32 + b'\n')
            with self.assertRaisesRegex(RuntimeError, 'зашифрован'):
                self.store.export(created['id'], PASSPHRASE)
            key.unlink()
            outside = self.state / 'other.neb'
            outside.touch(mode=0o600)
            outside.write_bytes(NATIVE_FIXTURE)
            key.symlink_to(outside)
            with self.assertRaises(RuntimeError):
                self.store.export(created['id'], PASSPHRASE)

    def test_tampered_installed_cli_is_not_used_for_wallet_creation(self):
        self.install_fixture()
        binary = self.state / 'wallets' / f'neb-wallet-v{wallet.VERSION}'
        binary.write_bytes(b'not the pinned release')
        self.assertFalse(self.store.status()['available'])
        with patch.object(wallet, '_run', side_effect=AssertionError('must not use tampered binary')):
            with self.assertRaises(RuntimeError):
                self.store.create('Rig', PASSPHRASE)


@unittest.skipUnless(sys.platform == 'linux' and os.environ.get('NEB_WALLET_TEST_ARCHIVE'),
                     'explicit offline fixture: set NEB_WALLET_TEST_ARCHIVE to the pinned public release')
class OfficialWalletAcceptance(unittest.TestCase):
    def test_native_pty_password_and_restore(self):
        if reason := wallet._platform_message():
            self.skipTest(reason)
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder).resolve()
            store = wallet.WalletStore(state)
            exact_password = ' пробная фраза NEB only '
            self.assertFalse(store.status()['available'])
            with patch.dict(os.environ, {'NEB_WALLET_PASSWORD': 'deliberately-not-the-fixture-passphrase'}), \
                    patch.object(wallet, '_download',
                                 return_value=Path(os.environ['NEB_WALLET_TEST_ARCHIVE']).read_bytes()):
                self.assertTrue(store.install()['available'])
                created = store.create('Synthetic offline test', exact_password)
                recovery = store.export(created['id'], exact_password)
                with self.assertRaises(RuntimeError):
                    store.export(created['id'], 'wrong synthetic test passphrase')
                restored = state / 'restored.neb'
                restored.write_bytes(base64.b64decode(recovery['backup'], validate=True))
                self.assertEqual(wallet._address(store.binary, restored, exact_password), created['address'])


if __name__ == '__main__':
    unittest.main()
