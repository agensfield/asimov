"""Explicit one-time installation of the fixed-path bridge; no root service."""
import argparse
import json
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import tempfile
import plistlib

import path_helper


def root_directory(directory):
    if not directory.exists():
        root_directory(directory.parent)
        directory.mkdir(mode=0o755)
        directory.chmod(0o755)
    info = directory.lstat()
    # /Applications is normally admin-group writable. The installed app itself
    # and all helper/policy directories remain root-owned and non-group-writable.
    writable = 0o002 if directory == Path('/Applications') else 0o022
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & writable:
        raise RuntimeError('Unsafe helper directory: ' + str(directory))


def replace_file(target, content, mode):
    root_directory(target.parent)
    fd, temporary = tempfile.mkstemp(dir=target.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def signing_requirement(application):
    subprocess.run(['/usr/bin/codesign', '--verify', '--strict', str(application)],
                   check=True, capture_output=True, text=True)
    result = subprocess.run(['/usr/bin/codesign', '--display', '--requirements', '-',
                             '--verbose=2', str(application)], check=True,
                            capture_output=True, text=True)
    output = result.stdout + result.stderr
    if 'Authority=Developer ID Application:' not in output:
        raise ValueError('Background app requires a Developer ID Application signature')
    match = re.search(r'(?m)^designated => (.+)$', output)
    if not match:
        raise ValueError('Missing designated signing requirement')
    return match.group(1)


def install_application(source):
    target = Path('/Applications/Asimov Background.app')
    info = plistlib.loads((source / 'Contents/Info.plist').read_bytes())
    if info.get('CFBundleIdentifier') != 'sh.agensfield.asimov' or info.get('CFBundleExecutable') != 'asimov-background':
        raise ValueError('Unexpected background application identity')
    requirement = signing_requirement(source)
    if target.exists() and signing_requirement(target) != requirement:
        raise ValueError('Existing background application has a different signing identity')
    # Stage and verify the complete signed app before replacing an existing one.
    # Signing runs as the normal user; the root installer copies only fixed files.
    root_directory(target.parent)
    with tempfile.TemporaryDirectory(dir=target.parent, prefix='.asimov-app-') as temporary:
        staged = Path(temporary) / target.name
        for relative, mode in [('Contents/MacOS/asimov-background', 0o755),
                               ('Contents/Info.plist', 0o644),
                               ('Contents/_CodeSignature/CodeResources', 0o644)]:
            item = source / relative
            if item.is_symlink() or not item.is_file():
                raise ValueError('Unexpected application member: ' + str(item))
            replace_file(staged / relative, item.read_bytes(), mode)
        if signing_requirement(staged) != requirement:
            raise RuntimeError('Staged app signature did not verify')
        backup = Path(temporary) / 'previous.app'
        if target.exists():
            target.rename(backup)
        try:
            staged.rename(target)
            if signing_requirement(target) != requirement:
                raise RuntimeError('Installed app signature did not verify')
        except Exception:
            if target.exists():
                target.rename(staged)
            if backup.exists():
                backup.rename(target)
            raise
    return str(target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--user', required=True)
    parser.add_argument('--background-app', type=Path)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError('Use explicit interactive sudo for helper installation')
    if not re.fullmatch(r'[a-z_][a-z0-9_-]*', args.user):
        raise ValueError('Unsupported account name')
    account = pwd.getpwnam(args.user)
    if account.pw_uid == 0:
        raise ValueError('The reconciler must run as a normal user')
    config = json.loads(args.config.read_text())
    if not config.get('fixed_path_exclusions'):
        raise ValueError('Enable fixed_path_exclusions in the reviewed config first')
    home = Path(account.pw_dir)

    def expand(value):
        if value.startswith('~/'):
            value = str(home / value[2:])
        return str(path_helper.canonical(value))

    policy = {'uid': account.pw_uid, 'roots': [expand(p) for p in config['roots']],
              'patterns': config['patterns'], 'protected_names': config['protected_names'],
              'protected_paths': [expand(p) for p in config.get('protected_paths', [])],
              'cache_paths': [expand(p) for p in config.get('cache_paths', [])],
              'cache_files': [expand(p) for p in config.get('cache_files', [])]}
    sudoers = Path('/private/etc/sudoers')
    included = re.search(r'(?m)^\s*(?:#|@)includedir\s+/private/etc/sudoers\.d\s*$',
                         sudoers.read_text())
    if not included:
        raise RuntimeError('Expected the standard sudoers.d include; sudoers was not changed')
    subprocess.run(['/usr/sbin/visudo', '-c'], check=True, capture_output=True, text=True)
    rule = (f'{args.user} ALL=(root) NOPASSWD: /usr/bin/python3 -I {path_helper.HELPER}\n')
    with tempfile.NamedTemporaryFile(mode='w') as stream:
        stream.write(rule)
        stream.flush()
        subprocess.run(['/usr/sbin/visudo', '-cf', stream.name], check=True,
                       capture_output=True, text=True)
    root_directory(path_helper.DIRECTORY)
    replace_file(path_helper.HELPER, Path(path_helper.__file__).read_bytes(), 0o755)
    replace_file(path_helper.POLICY, (json.dumps(policy, indent=2) + '\n').encode(), 0o600)
    replace_file(Path('/private/etc/sudoers.d/asimov-path-exclusions'), rule.encode(), 0o440)
    subprocess.run(['/usr/sbin/visudo', '-c'], check=True, capture_output=True, text=True)
    application = install_application(args.background_app) if args.background_app else None
    print(json.dumps({'helper': str(path_helper.HELPER), 'user': args.user,
                      'policy': str(path_helper.POLICY), 'application': application,
                      'new_service': False}))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
        raise SystemExit('asimov helper installation: ' + str(exc))
