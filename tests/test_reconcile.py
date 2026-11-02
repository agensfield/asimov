"""Reconciliation contracts, using real filesystem metadata and a fake tmutil."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "lib/asimov/reconcile.py"
sys.path.insert(0, str(SOURCE.parent))
import metadata

spec = importlib.util.spec_from_file_location("reconcile", SOURCE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
MARKER = module.MARKER


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.repo = self.base / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        (self.repo / "package.json").write_text("{}")
        self.dep = self.repo / "node_modules"
        self.dep.mkdir()
        self.tool = self.base / "tmutil"
        self.tool.write_text("""#!/usr/bin/env python3
import os,sys
sys.path.insert(0,ASIMOV_MODULE)
import metadata
key='com.apple.metadata:com_apple_backup_excludeItem'
cmd,p=sys.argv[1:]
if (os.path.join(p,'FAIL') and os.path.exists(os.path.join(p,'FAIL'))):
    sys.exit(2)
if cmd=='addexclusion':
    metadata.write(p,key,b'test')
else:
    try: metadata.read(p,key); flag='Excluded'
    except OSError: flag='Included'
    print('['+flag+'] '+p)
""".replace("ASIMOV_MODULE", repr(str(SOURCE.parent))))
        self.tool.chmod(0o755)
        self.config = {"roots": [str(self.repo)], "patterns": [
            {"directory": "node_modules", "sentinels": ["package.json"]}],
            "tmutil": str(self.tool), "state_dir": str(self.base / "state"),
            "executable": "/opt/homebrew/bin/asimov", "install_hooks": False}

    def engine(self, dry=False):
        return module.Reconciler(self.config, dry)

    def test_recreated_directory_is_excluded_again(self):
        self.engine().scan(self.repo)
        shutil.rmtree(self.dep)
        self.dep.mkdir()
        engine = self.engine()
        engine.scan(self.repo)
        self.assertEqual(engine.events[-1]["state"], "excluded")
        self.assertTrue(metadata.read(self.dep, MARKER))

    def test_removed_marker_is_repaired_and_failure_retried(self):
        self.engine().scan(self.repo)
        metadata.remove(self.dep, MARKER)
        (self.dep / "FAIL").touch()
        failed = self.engine()
        failed.scan(self.repo)
        self.assertEqual(failed.events[-1]["state"], "failed")
        (self.dep / "FAIL").unlink()
        self.engine().scan(self.repo)
        self.assertTrue(metadata.read(self.dep, MARKER))

    def test_git_tracked_fixture_is_preserved(self):
        tracked = self.dep / "fixture.js"
        tracked.touch()
        subprocess.run(["git", "-C", str(self.repo), "add", "-f", str(tracked)], check=True)
        engine = self.engine()
        engine.scan(self.repo)
        self.assertIn("protected", [e["state"] for e in engine.events])
        with self.assertRaises(OSError):
            metadata.read(self.dep, MARKER)

    def test_archive_and_external_symlink_are_preserved(self):
        archived = self.repo / "archive"
        archived.mkdir()
        (archived / "package.json").write_text("{}")
        (archived / "node_modules").mkdir()
        external = self.base / "external"
        external.mkdir()
        (self.repo / "linked").symlink_to(external, target_is_directory=True)
        self.engine().scan(self.repo)
        for path in [archived / "node_modules", external]:
            with self.assertRaises(OSError):
                metadata.read(path, MARKER)

    def test_manifest_appearing_later_is_discovered(self):
        (self.repo / "package.json").unlink()
        self.engine().scan(self.repo)
        (self.repo / "package.json").write_text("{}")
        self.engine().scan(self.repo)
        self.assertTrue(metadata.read(self.dep, MARKER))

    def test_dry_run_does_not_mark(self):
        engine = self.engine(True)
        engine.scan(self.repo)
        self.assertEqual(engine.events[-1]["state"], "would_exclude")
        with self.assertRaises(OSError):
            metadata.read(self.dep, MARKER)

    def test_cli_failure_status_and_receipt(self):
        config = self.base / "config.json"
        config.write_text(json.dumps(self.config))
        (self.dep / "FAIL").touch()
        result = subprocess.run([sys.executable, str(SOURCE), "--config", str(config),
                                 "--json"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(json.loads(result.stdout)["counts"]["failed"], 1)
        self.assertTrue((self.base / "state/latest.json").exists())

    def test_concurrent_runs_serialize(self):
        config = self.base / "config.json"
        config.write_text(json.dumps(self.config))
        args = [sys.executable, str(SOURCE), "--config", str(config), "--json"]
        first = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        second = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for process in [first, second]:
            out, err = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, err)
            self.assertNotIn("failed", json.loads(out)["counts"])
        self.assertEqual(len(list((self.base / "state/receipts").glob("*.json"))), 2)

    def test_hook_chains_existing_script_and_reconciles(self):
        import shlex
        self.config["install_hooks"] = True
        shim = self.base / "asimov"
        shim.write_text("#!/bin/sh\nshift\nexec " +
                        shlex.join([sys.executable, str(SOURCE)]) + ' "$@"\n')
        shim.chmod(0o755)
        self.config["executable"] = str(shim)
        config = self.base / "config.json"
        config.write_text(json.dumps(self.config))
        hook = self.repo / ".git/hooks/post-checkout"
        log = self.base / "hook.log"
        hook.write_text("#!/bin/sh\necho old >> " + shlex.quote(str(log)) + "\n")
        hook.chmod(0o755)
        engine = self.engine()
        engine.scan(self.repo)
        engine.install_hooks(config)
        engine.install_hooks(config)
        self.assertTrue(hook.with_name("post-checkout.pre-asimov").exists())
        os.removexattr(self.dep, MARKER) if hasattr(os, "removexattr") else metadata.remove(self.dep, MARKER)
        result = subprocess.run([str(hook), "old", "new", "1"], cwd=self.repo,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(log.read_text(), "old\n")
        self.assertTrue(metadata.read(self.dep, MARKER))

    def test_explicit_cache_target_cannot_override_protection(self):
        self.config["protected_paths"] = [str(self.dep)]
        engine = self.engine()
        engine.exclude(self.dep)
        self.assertEqual(engine.events[-1]["state"], "protected")

    def test_explicit_cache_files_reconcile_recreation_and_skip_missing_sidecars(self):
        cache = self.base / "history-index.sqlite"
        cache.write_bytes(b"derived index")
        self.config["cache_files"] = [str(cache), str(cache) + "-wal"]
        config = self.base / "config.json"
        config.write_text(json.dumps(self.config))
        args = [sys.executable, str(SOURCE), "--config", str(config), "--json"]
        preview = subprocess.run(args + ["--dry-run"], capture_output=True, text=True)
        self.assertEqual(preview.returncode, 0, preview.stderr)
        with self.assertRaises(OSError):
            metadata.read(cache, MARKER)
        for _ in range(2):
            result = subprocess.run(args, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            events = json.loads(result.stdout)["events"]
            self.assertIn({"path": str(cache), "state": "excluded", "reason": ""}, events)
            self.assertEqual(cache.read_bytes(), b"derived index")
            self.assertTrue(metadata.read(cache, MARKER))
            cache.unlink()
            cache.write_bytes(b"derived index")

    def test_file_allowlist_preserves_protection_symlinks_and_directory_contract(self):
        cache = self.base / "history-index.sqlite"
        cache.write_bytes(b"derived index")
        self.config["protected_paths"] = [str(cache)]
        engine = self.engine()
        engine.exclude(cache, regular_file=True)
        self.assertEqual(engine.events[-1]["state"], "protected")
        self.config["protected_paths"] = []
        linked = self.base / "linked.sqlite"
        linked.symlink_to(cache)
        engine = self.engine()
        engine.exclude(linked, regular_file=True)
        self.assertEqual(engine.events[-1]["state"], "vanished")
        engine.exclude(cache)
        self.assertEqual(engine.events[-1]["state"], "vanished")
        with self.assertRaises(OSError):
            metadata.read(cache, MARKER)


    def test_bare_cli_uses_verified_engine(self):
        config = self.base / "config.json"
        config.write_text(json.dumps(self.config))
        launcher = SOURCE.parents[2] / "bin/asimov"
        result = subprocess.run([str(launcher), "--config", str(config),
                                 "--dry-run", "--json"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["counts"]["would_exclude"], 1)



if __name__ == "__main__":
    unittest.main()
