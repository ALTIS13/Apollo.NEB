import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parent


class InstallerPathTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name).resolve()
        self.prefix = self.home / '.local'
        self.base = self.prefix / 'lib' / 'apollo-neb'
        self.launcher = self.prefix / 'bin' / 'apollo-neb'
        self.unit = self.home / '.config/systemd/user/apollo-neb.service'

    def check(self, *, prefix=None, service=False):
        return subprocess.run([sys.executable, '-I', str(SOURCE / 'neb_install_check.py'),
                               str(prefix or self.prefix), str(self.home), str(int(service))],
                              capture_output=True, text=True, encoding='utf-8')

    def assert_refusal(self, result, target):
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f'Refusing installer target: {target}', result.stderr)

    def symlink(self, path, target, *, directory=False):
        try:
            path.symlink_to(target, target_is_directory=directory)
        except OSError as error:
            if os.name != 'nt':
                raise
            self.skipTest(f'Windows symbolic links unavailable ({error.winerror})')

    def test_fresh_path_is_accepted_without_mutation(self):
        result = self.check(service=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.prefix.exists())
        self.assertFalse(self.unit.parent.exists())

    def test_lexical_prefix_alias_is_refused(self):
        alias = str(self.prefix) + os.sep + '..' + os.sep + self.prefix.name
        self.assert_refusal(self.check(prefix=alias), 'prefix')

    def test_bin_symlink_is_refused(self):
        target = self.home / 'unrelated-bin'
        target.mkdir()
        self.prefix.mkdir()
        self.symlink(self.launcher.parent, target, directory=True)
        self.assert_refusal(self.check(), 'bin')

    def test_existing_launcher_of_another_program_is_preserved(self):
        self.launcher.parent.mkdir(parents=True)
        self.launcher.write_bytes(b'foreign executable\n')
        self.assert_refusal(self.check(), 'launcher')
        self.assertEqual(self.launcher.read_bytes(), b'foreign executable\n')
        self.assertFalse(self.base.exists())

    def test_existing_service_of_another_program_is_preserved(self):
        self.unit.parent.mkdir(parents=True)
        self.unit.write_bytes(b'foreign unit\n')
        self.assert_refusal(self.check(service=True), 'unit')
        self.assertEqual(self.unit.read_bytes(), b'foreign unit\n')

    def test_current_pointing_outside_code_base_is_refused(self):
        self.base.mkdir(parents=True)
        outside = self.home / 'release.abcdefgh'
        outside.mkdir()
        self.symlink(self.base / 'current', outside, directory=True)
        self.assert_refusal(self.check(), 'current')

    def test_service_tree_redirected_by_parent_symlink_is_refused(self):
        elsewhere = self.home / 'unrelated-config'
        elsewhere.mkdir()
        self.symlink(self.home / '.config', elsewhere, directory=True)
        self.assert_refusal(self.check(service=True), 'service directory')

    def test_existing_neb_artifacts_allow_upgrade(self):
        self.launcher.parent.mkdir(parents=True)
        self.launcher.write_bytes((SOURCE / 'apollo-neb').read_bytes())
        self.launcher.chmod(0o700)
        self.unit.parent.mkdir(parents=True)
        self.unit.write_bytes((SOURCE / 'apollo-neb.service').read_bytes())
        self.unit.chmod(0o644)
        release = self.base / 'release.abcdefgh'
        release.mkdir(parents=True, mode=0o700)
        (release / 'neb.py').write_bytes((SOURCE / 'neb.py').read_bytes())
        self.symlink(self.base / 'current', release.name, directory=True)
        result = self.check(service=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(sys.platform == 'linux' and os.geteuid() != 0, 'rootless Linux installer')
    def test_installer_preserves_existing_directory_modes_and_blocks_foreign_launcher(self):
        self.base.mkdir(parents=True)
        self.base.chmod(0o750)
        self.launcher.parent.mkdir()
        self.launcher.write_bytes(b'unrelated program\n')
        env = dict(os.environ, HOME=str(self.home), XDG_CONFIG_HOME=str(self.home / '.config'),
                   XDG_STATE_HOME=str(self.home / '.state'))
        command = ['/bin/sh', str(SOURCE / 'install.sh'), '--', '--password-stdin']

        def install():
            return subprocess.run(command, env=env, input='fixture-only installer password\n',
                                  capture_output=True, text=True, encoding='utf-8', timeout=15)

        self.assert_refusal(install(), 'launcher')
        self.assertEqual(self.launcher.read_bytes(), b'unrelated program\n')
        self.assertFalse((self.home / '.config').exists())
        self.launcher.unlink()
        for _ in range(2):
            result = install()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(stat.S_IMODE(self.base.stat().st_mode), 0o750)
            self.assertTrue((self.base / 'current').resolve(strict=True).is_dir())


if __name__ == '__main__':
    unittest.main()
