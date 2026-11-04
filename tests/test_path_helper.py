"""Native-list contracts: markers alone must never satisfy fixed-path policy."""
import copy
import json
import os
import plistlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / 'lib/asimov'
sys.path.insert(0, str(SOURCE))
import path_helper
import reconcile
import metadata


class FakeNative:
    def __init__(self, paths=()):
        self.current = set(paths)
        self.calls = []
        self.fail_add = False

    def paths(self):
        return self.current.copy()

    def change(self, verb, paths):
        self.calls.append((verb, paths))
        if self.fail_add:
            raise RuntimeError('native failure')
        if verb == 'addexclusion':
            self.current.update(paths)
        else:
            self.current.difference_update(paths)


class PathHelperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.project = self.root / '.worktrees/feature'
        self.project.mkdir(parents=True)
        (self.project / 'package.json').write_text('{}')
        self.dep = self.project / 'node_modules'
        self.dep.mkdir()
        self.policy = {'uid': os.getuid(), 'roots': [str(self.root)],
                       'patterns': [{'directory': 'node_modules', 'sentinels': ['package.json']}],
                       'protected_paths': [], 'protected_names': ['.git', 'fixtures', 'archive'],
                       'cache_paths': [], 'cache_files': []}
        self.state = {}
        self.native = FakeNative()

    def save(self, value):
        self.state = copy.deepcopy(value)

    def run_helper(self, paths=None, scopes=None, complete=True):
        return path_helper.reconcile(
            {'paths': paths if paths is not None else [str(self.dep)],
             'scopes': scopes if scopes is not None else [str(self.root)],
             'complete': complete}, self.policy, self.state, self.native, self.save)

    def test_hidden_path_gets_native_entry_and_remains_idempotent(self):
        events = self.run_helper()
        self.assertEqual(events[0]['state'], 'fixed_registered')
        self.assertEqual(self.native.current, {str(self.dep)})
        events = self.run_helper()
        self.assertEqual(events[0]['state'], 'fixed_verified')
        self.assertEqual(len(self.native.calls), 1)

    def test_native_readback_uses_preferences_api_not_delayed_disk_plist(self):
        result = subprocess.CompletedProcess([], 0,
            plistlib.dumps({'SkipPaths': [str(self.dep)]}), b'')
        with patch.object(path_helper.subprocess, 'run', return_value=result) as run:
            self.assertEqual(path_helper.Native().paths(), {str(self.dep)})
        self.assertEqual(run.call_args.args[0], ['/usr/bin/defaults', 'export',
            '/Library/Preferences/com.apple.TimeMachine', '-'])

    def test_identity_preflight_is_fixed_read_only_and_reads_zero_content(self):
        with patch.object(path_helper.os, 'open', return_value=123) as opened, \
                patch.object(path_helper.os, 'close') as closed, \
                patch.object(path_helper.os, 'read') as read:
            self.assertEqual(path_helper.identity_probe(), {'protected_open_allowed': True})
        opened.assert_called_once_with('/Library/Application Support/com.apple.TCC/TCC.db', os.O_RDONLY)
        closed.assert_called_once_with(123)
        read.assert_not_called()

    def test_retirement_removes_only_owned_entries_and_only_in_scope(self):
        manual = str(self.root / 'manual/node_modules')
        self.native.current.add(manual)
        self.run_helper()
        self.native.current.add(str(self.root / 'unrelated'))
        self.run_helper(paths=[], scopes=[str(self.root / 'another-project')])
        self.assertIn(str(self.dep), self.native.current)
        self.run_helper(paths=[], complete=False)
        self.assertIn(str(self.dep), self.native.current)
        events = self.run_helper(paths=[])
        self.assertEqual(events[0]['state'], 'fixed_removed')
        self.assertEqual(self.native.current, {manual, str(self.root / 'unrelated')})

    def test_preexisting_manual_target_is_never_adopted(self):
        self.native.current.add(str(self.dep))
        self.run_helper()
        self.assertEqual(self.state['managed_paths'], [])
        self.run_helper(paths=[])
        self.assertIn(str(self.dep), self.native.current)

    def test_absent_allowlisted_sidecars_stay_registered_across_recreation(self):
        item = self.root / 'index.sqlite-wal'
        self.policy['cache_files'] = [str(item)]
        self.run_helper(paths=[str(item)])
        item.write_text('derived state')
        self.run_helper(paths=[str(item)])
        item.unlink()
        self.run_helper(paths=[str(item)])
        self.assertIn(str(item), self.native.current)
        self.assertEqual(len(self.native.calls), 1)

    def test_rejects_whole_worktrees_source_protection_and_symlink_escape(self):
        outside = self.root / 'outside'
        outside.mkdir()
        linked = self.project / 'linked'
        linked.symlink_to(outside, target_is_directory=True)
        cases = [str(self.project), str(self.project / 'package.json'),
                 str(linked / 'node_modules')]
        self.policy['protected_paths'] = [str(self.dep)]
        cases.append(str(self.dep))
        for item in cases:
            with self.subTest(item=item), self.assertRaises(ValueError):
                self.run_helper(paths=[item])
        self.assertEqual(self.native.calls, [])

    def test_cache_allowlist_does_not_override_worktree_root_guard(self):
        (self.project / '.git').write_text('gitdir: somewhere')
        self.policy['cache_paths'] = [str(self.project)]
        with self.assertRaises(ValueError):
            self.run_helper(paths=[str(self.project)])

    def test_missing_directories_do_not_abort_other_registrations(self):
        gone = self.root / '.worktrees/retired/node_modules'
        events = self.run_helper(paths=[str(self.dep), str(gone)])
        self.assertIn(str(self.dep), self.native.current)
        self.assertIn('fixed_vanished', [e['state'] for e in events])

    def test_native_failure_keeps_recovery_ownership_and_does_not_report_success(self):
        self.native.fail_add = True
        with self.assertRaises(RuntimeError):
            self.run_helper()
        self.assertEqual(self.state['managed_paths'], [str(self.dep)])
        self.native.fail_add = False
        self.assertEqual(self.run_helper()[0]['state'], 'fixed_registered')

    def test_marked_candidate_still_requests_native_registration_and_reports_failure(self):
        engine = reconcile.Reconciler({'roots': [str(self.root)],
            'patterns': self.policy['patterns'], 'fixed_path_exclusions': True})
        engine.fixed_targets.add(str(self.dep))
        with patch.object(reconcile, 'command') as command:
            command.return_value = subprocess.CompletedProcess([], 1, '', 'helper unavailable')
            engine.register_fixed_paths([self.root])
            request = json.loads(command.call_args.kwargs['input'])
        self.assertEqual(request['paths'], [str(self.dep)])
        self.assertEqual(engine.events[-1]['state'], 'failed')
        self.assertIn('helper unavailable', engine.events[-1]['reason'])
        dry = reconcile.Reconciler(engine.config, dry=True)
        dry.fixed_targets.add(str(self.dep))
        with patch.object(reconcile, 'command') as command:
            dry.register_fixed_paths([self.root])
            command.assert_not_called()
        self.assertEqual(dry.events[-1]['state'], 'would_verify_fixed')

    def test_marker_verification_does_not_skip_native_registration_or_git_protection(self):
        subprocess.run(['git', 'init', '-q', str(self.project)], check=True)
        metadata.write(self.dep, reconcile.MARKER, b'existing portable marker')
        engine = reconcile.Reconciler({'roots': [str(self.root)],
            'patterns': self.policy['patterns'], 'fixed_path_exclusions': True})
        original = reconcile.command

        def command(args, **kwargs):
            if args[0] == '/usr/bin/tmutil':
                return subprocess.CompletedProcess(args, 0, '[Excluded] ' + args[-1], '')
            if args[0] == '/usr/bin/sudo':
                events = path_helper.reconcile(json.loads(kwargs['input']), self.policy,
                    self.state, self.native, self.save)
                return subprocess.CompletedProcess(args, 0, json.dumps({'events': events}), '')
            return original(args, **kwargs)

        with patch.object(reconcile, 'command', side_effect=command):
            engine.scan(self.root)
            engine.register_fixed_paths([self.root])
            self.assertIn('already_excluded', [e['state'] for e in engine.events])
            self.assertIn('fixed_registered', [e['state'] for e in engine.events])
            (self.dep / 'fixture.js').write_text('tracked source')
            subprocess.run(['git', '-C', str(self.project), 'add', '-f', 'node_modules/fixture.js'],
                           check=True)
            engine = reconcile.Reconciler(engine.config)
            engine.scan(self.root)
            engine.register_fixed_paths([self.root])
        self.assertIn('protected', [e['state'] for e in engine.events])
        self.assertNotIn(str(self.dep), self.native.current)


if __name__ == '__main__':
    unittest.main()
