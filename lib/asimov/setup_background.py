"""Prepare a Developer ID-signed background app, then install with explicit sudo."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import pwd
import shutil
import subprocess
import sys
import tempfile

APP = Path('/Applications/Asimov Background.app')
BUNDLE_ID = 'sh.agensfield.asimov'
MODULE = Path(__file__).resolve().parent


def prepare(destination, launcher, executable, identity):
    contents = destination / 'Contents'
    (contents / 'MacOS').mkdir(parents=True)
    shutil.copyfile(launcher, contents / 'MacOS/asimov-background')
    (contents / 'MacOS/asimov-background').chmod(0o755)
    info = {'CFBundleIdentifier': BUNDLE_ID, 'CFBundleExecutable': 'asimov-background',
            'CFBundleName': 'Asimov Background', 'CFBundleDisplayName': 'Asimov Background',
            'CFBundlePackageType': 'APPL', 'CFBundleVersion': '1',
            'CFBundleShortVersionString': '1.0', 'LSBackgroundOnly': True,
            'AsimovExecutable': str(executable),
            'AsimovLauncherSourceSHA256': hashlib.sha256((MODULE / 'background.m').read_bytes()).hexdigest()}
    (contents / 'Info.plist').write_bytes(plistlib.dumps(info))
    subprocess.run(['/usr/bin/codesign', '--force', '--options', 'runtime', '--timestamp',
                    '--sign', identity, str(destination)], check=True)
    subprocess.run(['/usr/bin/codesign', '--verify', '--strict', str(destination)], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--identity', required=True, help='Developer ID Application identity or fingerprint')
    parser.add_argument('--config', type=Path, default=Path.home() / '.config/asimov/managed.json')
    args = parser.parse_args()
    if os.geteuid() == 0:
        raise RuntimeError('Prepare/sign as the normal user; installation invokes sudo explicitly')
    prefix = MODULE.parents[1]
    launcher = prefix / 'bin/asimov-background'
    if not launcher.is_file():
        raise RuntimeError('Native launcher missing; install the current Homebrew formula first')
    executable = shutil.which('asimov')
    if not executable:
        raise RuntimeError('Cannot locate the installed Asimov entry point')
    user = pwd.getpwuid(os.getuid()).pw_name
    with tempfile.TemporaryDirectory(prefix='asimov-signed-') as temporary:
        staged = Path(temporary) / APP.name
        prepare(staged, launcher, Path(executable).absolute(), args.identity)
        subprocess.run(['/usr/bin/sudo', sys.executable, str(MODULE / 'install_path_helper.py'),
                        '--config', str(args.config.expanduser().resolve()), '--user', user,
                        '--background-app', str(staged)], check=True)
    register = '/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister'
    subprocess.run([register, '-f', str(APP)], check=True)
    print(json.dumps({'application': str(APP), 'bundle_identifier': BUNDLE_ID,
                      'fda_state': 'not_checked', 'next_step': 'Verify launchd attribution, then enable this app in Full Disk Access'}))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        raise SystemExit('asimov background setup: ' + str(exc))
