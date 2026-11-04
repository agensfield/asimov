"""Native responsible-process launcher and signed-bundle preparation contracts."""
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / 'lib/asimov'
sys.path.insert(0, str(MODULE))
import setup_background


@unittest.skipUnless(sys.platform == 'darwin', 'macOS native launcher')
class BackgroundTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.tmp.name).resolve()
        cls.child = cls.base / 'reconciler'
        cls.child.write_text('#!/bin/sh\nprintf "%s\\n" "$PPID"\nexit "${ASIMOV_TEST_EXIT:-0}"\n')
        cls.child.chmod(0o755)
        cls.binary = cls.base / 'asimov-background'
        subprocess.run(['/usr/bin/clang', '-fobjc-arc', '-Wall', '-Wextra', '-Werror',
            '-framework', 'Foundation', str(MODULE / 'background.m'),
            '-DASIMOV_EXECUTABLE="' + str(cls.child) + '"',
            '-DASIMOV_NOTIFICATION_EXECUTABLE="/usr/bin/true"', '-o', str(cls.binary)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_launcher_remains_parent_and_preserves_child_exit_status(self):
        for status in [0, 7]:
            process = subprocess.Popen([str(self.binary)], text=True, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env={**os.environ, 'ASIMOV_TEST_EXIT': str(status)})
            stdout, stderr = process.communicate(timeout=15)
            self.assertEqual(int(stdout.strip()), process.pid)
            self.assertEqual(process.returncode, status)
            if status:
                self.assertIn('last-failure.json', stderr)

    def test_arbitrary_commands_are_not_forwarded(self):
        result = subprocess.run([str(self.binary), '/usr/bin/true'], text=True, capture_output=True)
        self.assertEqual(result.returncode, 64)
        self.assertEqual(result.stdout, '')

    def test_bundle_preparation_binds_native_executable_and_stable_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = Path(temporary) / 'Asimov Background.app'
            with patch.object(setup_background.subprocess, 'run') as run:
                setup_background.prepare(app, self.binary, Path('/opt/homebrew/bin/asimov'), 'signer')
            info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
            self.assertEqual(info['CFBundleIdentifier'], 'sh.agensfield.asimov')
            self.assertEqual(info['CFBundleExecutable'], 'asimov-background')
            self.assertEqual(info['AsimovExecutable'], '/opt/homebrew/bin/asimov')
            self.assertEqual((app / 'Contents/MacOS/asimov-background').read_bytes(), self.binary.read_bytes())
            self.assertIn('--options', run.call_args_list[0].args[0])
            self.assertIn('runtime', run.call_args_list[0].args[0])
            self.assertIn('--timestamp', run.call_args_list[0].args[0])
            self.assertIn('--verify', run.call_args_list[1].args[0])


if __name__ == '__main__':
    unittest.main()
