#!/usr/bin/python3
"""Restricted, root-owned bridge to tmutil's fixed-path exclusion mode.

The user reconciler discovers rebuildable content. This helper never runs Git,
imports user modules, scans repositories, or executes configurable commands.
"""
import fcntl
import json
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import sys
import tempfile

HELPER = Path('/Library/PrivilegedHelperTools/agensfield.asimov.path-exclusions.py')
DIRECTORY = Path('/Library/Application Support/Agensfield/Asimov')
POLICY = DIRECTORY / 'path-policy.json'
STATE = DIRECTORY / 'path-state.json'
PREFERENCES = Path('/Library/Preferences/com.apple.TimeMachine.plist')


def secure_file(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise RuntimeError('Expected a root-owned, non-writable regular file: ' + str(path))


def atomic_json(path, value):
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, indent=2)
            stream.write('\n')
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def descendant(item, parent):
    return item == parent or parent in item.parents


def canonical(value):
    if not isinstance(value, str) or not value.startswith('/') or '\0' in value:
        raise ValueError('Expected an absolute path')
    item = Path(value)
    if str(item) != value or item.resolve() != item:
        raise ValueError('Noncanonical path or symlink: ' + value)
    return item


def validate_target(value, policy):
    item = canonical(value)
    protected = [Path(p) for p in policy['protected_paths']]
    if any(descendant(item, p) for p in protected) or any(
            part in policy['protected_names'] for part in item.parts):
        raise ValueError('Protected path: ' + value)
    files = set(policy['cache_files'])
    caches = set(policy['cache_paths'])
    if value in files or value in caches:
        # Reviewed exact paths remain registered while an installer/database
        # temporarily removes them, so recreation does not open a timing gap.
        if not item.exists():
            return item
        valid = item.is_file() if value in files else item.is_dir()
        if value in caches and (item / '.git').exists():
            raise ValueError('Cannot exclude a repository/worktree root: ' + value)
    else:
        if not any(descendant(item, Path(p)) and item != Path(p) for p in policy['roots']):
            raise ValueError('Outside configured roots: ' + value)
        patterns = {p['directory']: p for p in policy['patterns']}
        pattern = patterns.get(item.name)
        if not pattern:
            raise ValueError('Unapproved directory name: ' + value)
        if not item.exists():
            return None
        if not any((item.parent / s).is_file() for s in pattern['sentinels']):
            raise ValueError('No approved directory/sentinel pair: ' + value)
        if pattern.get('inside') and not (item / pattern['inside']).is_file():
            raise ValueError('Missing directory sentinel: ' + value)
        valid = item.is_dir()
    if not valid or item.stat().st_uid != policy['uid']:
        raise ValueError('Wrong target type or owner: ' + value)
    return item


class Native:
    def paths(self):
        # cfprefsd can defer the on-disk plist write after tmutil exits. Read
        # through the native preferences API rather than that stale disk file.
        result = subprocess.run(['/usr/bin/defaults', 'export', str(PREFERENCES.with_suffix('')), '-'],
                                capture_output=True, check=True, timeout=30)
        value = plistlib.loads(result.stdout)
        return set(value.get('SkipPaths', []))

    def change(self, verb, paths):
        for offset in range(0, len(paths), 64):
            result = subprocess.run(['/usr/bin/tmutil', verb, '-p', *paths[offset:offset + 64]],
                                    capture_output=True, text=True, timeout=120)
            if result.returncode:
                raise RuntimeError(result.stderr.strip() or result.stdout.strip() or
                                   'tmutil fixed-path update failed')


def identity_probe():
    descriptor = os.open('/Library/Application Support/com.apple.TCC/TCC.db', os.O_RDONLY)
    try:
        return {'protected_open_allowed': True}
    finally:
        os.close(descriptor)


def reconcile(request, policy, state, native, save):
    if set(request) != {'paths', 'scopes', 'complete'}:
        raise ValueError('Invalid request fields')
    if not isinstance(request['paths'], list) or len(request['paths']) > 4096:
        raise ValueError('Invalid path list')
    if not isinstance(request['scopes'], list) or len(request['scopes']) > 256:
        raise ValueError('Invalid scope list')
    if not isinstance(request['complete'], bool):
        raise ValueError('Invalid discovery completeness flag')
    validated = {p: validate_target(p, policy) for p in request['paths']}
    targets = sorted({str(p) for p in validated.values() if p is not None})
    scopes = [canonical(p) for p in request['scopes']]
    for scope in scopes:
        if not (any(descendant(scope, Path(p)) for p in policy['roots']) or
                str(scope) in policy['cache_files'] + policy['cache_paths']):
            raise ValueError('Invalid reconciliation scope: ' + str(scope))
    if any(not any(descendant(Path(p), scope) for scope in scopes) for p in targets):
        raise ValueError('Target outside requested scope')
    current = native.paths()
    managed = set(state.get('managed_paths', []))
    additions = sorted(set(targets) - current)
    # Never adopt pre-existing manual exclusions. Persist intent before invoking
    # tmutil so interrupted/partial batches can be recovered on the next sweep.
    managed.update(additions)
    save({'managed_paths': sorted(managed)})
    if additions:
        native.change('addexclusion', additions)
    retirements = sorted(p for p in managed - set(targets)
                         if request['complete'] and any(descendant(Path(p), s) for s in scopes))
    # An interrupted sweep can leave ownership for an entry already removed.
    # Do not pass absent entries to tmutil: on Seele, removing an absent path
    # also dropped a newly registered, unrelated entry. Retire the ledger only.
    removals = sorted(set(retirements) & current)
    if removals:
        native.change('removeexclusion', removals)
    final = native.paths()
    missing = set(targets) - final
    remaining = set(retirements) & final
    lost = current - set(removals) - final
    if missing or remaining or lost:
        raise RuntimeError('Native fixed-path preferences did not verify: ' + json.dumps({
            'missing_targets': sorted(missing), 'remaining_retirements': sorted(remaining),
            'lost_existing_entries': sorted(lost)}))
    managed.difference_update(retirements)
    save({'managed_paths': sorted(managed)})
    return [{'path': p, 'state': 'fixed_registered' if p in additions else 'fixed_verified',
             'reason': 'Verified in native SkipPaths preferences'} for p in targets] + [
        {'path': p, 'state': 'fixed_removed',
         'reason': 'Retired Asimov-owned path entry' if p in removals else
                   'Retired already-absent Asimov-owned path entry'}
        for p in retirements] + [
        {'path': p, 'state': 'fixed_vanished', 'reason': 'Removed before native registration'}
        for p, value in validated.items() if value is None]


def main():
    if os.geteuid() != 0 or len(sys.argv) != 1:
        raise RuntimeError('Run only through the configured sudo entry, without arguments')
    secure_file(POLICY)
    policy = json.loads(POLICY.read_text())
    if os.environ.get('SUDO_UID') != str(policy['uid']):
        raise RuntimeError('Caller does not match installed policy')
    raw = sys.stdin.buffer.read(1048577)
    if len(raw) > 1048576:
        raise ValueError('Request too large')
    request = json.loads(raw)
    if set(request) == {'check_identity'} and request['check_identity'] is True:
        # FDA is an OS-enforced prerequisite. This fixed read-only open checks
        # the caller's actual launch context; no database content is read.
        print(json.dumps(identity_probe()))
        return
    lock_path = DIRECTORY / 'path-state.lock'
    with lock_path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if STATE.exists():
            secure_file(STATE)
            state = json.loads(STATE.read_text())
        else:
            state = {}
        events = reconcile(request, policy, state, Native(), lambda v: atomic_json(STATE, v))
        print(json.dumps({'events': events}))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as exc:
        print('asimov path helper: ' + str(exc), file=sys.stderr)
        sys.exit(1)
