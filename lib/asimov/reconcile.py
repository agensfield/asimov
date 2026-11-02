#!/usr/bin/env python3
"""Verified, bounded reconciliation for the Agensfield Asimov distribution."""
import argparse
import collections
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

import metadata

MARKER = "com.apple.metadata:com_apple_backup_excludeItem"
VERSION = "0.13.0"
DEFAULT_CONFIG = Path.home() / ".config/asimov/managed.json"


def command(args, **kwargs):
    return subprocess.run(args, capture_output=True, text=True, timeout=120, **kwargs)


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Reconciler:
    def __init__(self, config, dry=False, hooks=True):
        self.config = config
        self.dry = dry
        self.hooks = hooks and config.get("install_hooks", False)
        self.roots = [Path(p).expanduser().resolve() for p in config["roots"]]
        self.protected = [Path(p).expanduser().resolve()
                          for p in config.get("protected_paths", [])]
        self.names = set(config.get("protected_names",
                         [".git", "archive", "archives", "backups", "fixture", "fixtures"]))
        self.patterns = config["patterns"]
        self.events = []
        self.repositories = set()
        self.tracked = {}
        self.tmutil = config.get("tmutil", "/usr/bin/tmutil")
        self.git = config.get("git", "/usr/bin/git")

    def record(self, path, state, reason=""):
        self.events.append({"path": str(path), "state": state, "reason": reason})

    def protected_path(self, path):
        return any(path == p or p in path.parents for p in self.protected) or any(
            part in self.names for part in path.parts)

    def repository(self, path):
        result = command([self.git, "-C", str(path), "rev-parse", "--show-toplevel"])
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "Cannot identify repository")
        repo = Path(result.stdout.strip()).resolve()
        if repo not in self.tracked:
            # NUL output preserves spaces/newlines in tracked file names.
            result = subprocess.run(
                [self.git, "-C", str(repo), "ls-files", "-z"],
                capture_output=True, check=True, timeout=120)
            self.tracked[repo] = result.stdout.split(b"\0")
        self.repositories.add(repo)
        return repo

    def eligible(self, path, pattern):
        if self.protected_path(path):
            return False
        if not any((path.parent / sentinel).is_file()
                   for sentinel in pattern["sentinels"]):
            return False
        if pattern.get("inside") and not (path / pattern["inside"]).is_file():
            return False
        try:
            repo = self.repository(path.parent)
        except RuntimeError as error:
            if "not a git repository" in str(error):
                self.record(path, "protected", "No repository; rebuildability not established")
                return False
            raise
        prefix = os.fsencode(str(path.relative_to(repo))) + b"/"
        if any(name.startswith(prefix) for name in self.tracked[repo]):
            self.record(path, "protected", "Git-tracked content")
            return False
        return True

    def scan(self, root):
        if not root.exists():
            self.record(root, "vanished", "Scan root no longer exists")
            return
        if root.is_symlink() or self.protected_path(root):
            self.record(root, "protected", "Protected root or symlink")
            return
        patterns = {p["directory"]: p for p in self.patterns}
        stack = [root]
        while stack:
            parent = stack.pop()
            try:
                if (parent / ".git").exists():
                    self.repository(parent)
                with os.scandir(parent) as entries:
                    for entry in entries:
                        path = Path(entry.path)
                        if entry.is_symlink():
                            continue
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                        if self.protected_path(path):
                            continue
                        pattern = patterns.get(entry.name)
                        if pattern:
                            if self.eligible(path, pattern):
                                self.exclude(path)
                            # Never descend into dependency/build trees,
                            # even when absent a manifest or explicitly protected.
                            continue
                        stack.append(path)
            except FileNotFoundError:
                self.record(parent, "vanished", "Removed during discovery")
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                self.record(parent, "failed", str(error))

    def exclude(self, path):
        try:
            if self.protected_path(path):
                self.record(path, "protected", "Protected exclusion target")
                return
            if path.is_symlink() or not path.is_dir():
                self.record(path, "vanished", "Removed or replaced during reconciliation")
                return
            identity = path.stat()
            status = command([self.tmutil, "isexcluded", str(path)])
            if status.returncode or not status.stdout.startswith(("[Included]", "[Excluded]")):
                raise RuntimeError("Cannot read effective exclusion: " + status.stderr.strip())
            excluded = status.stdout.startswith("[Excluded]")
            # Stamp even fixed-path/parent-excluded candidates locally so Ramiz
            # inheritance can propagate this directory's own exclusion intent.
            try:
                marked = bool(metadata.read(path, MARKER))
            except OSError:
                marked = False
            if excluded and marked:
                self.record(path, "already_excluded")
                return
            if self.dry:
                self.record(path, "would_mark" if excluded else "would_exclude")
                return
            before = (identity.st_dev, identity.st_ino)
            result = command([self.tmutil, "addexclusion", str(path)])
            if result.returncode:
                raise RuntimeError(result.stdout.strip() + " " + result.stderr.strip())
            now = path.stat()
            if path.is_symlink() or (now.st_dev, now.st_ino) != before:
                raise RuntimeError("Directory identity changed while setting exclusion")
            verify = command([self.tmutil, "isexcluded", str(path)])
            if verify.returncode or not verify.stdout.startswith("[Excluded]"):
                raise RuntimeError("Exclusion did not verify")
            if not metadata.read(path, MARKER):
                raise RuntimeError("Local exclusion marker did not verify")
            self.record(path, "marked" if excluded else "excluded")
        except FileNotFoundError:
            self.record(path, "vanished", "Removed during exclusion")
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            self.record(path, "failed", str(error))

    def install_hooks(self, config_path):
        if not self.hooks or self.dry:
            return
        executable = self.config["executable"]
        for repo in sorted(self.repositories):
            hook_roots = [Path(p).expanduser().resolve()
                          for p in self.config.get("hook_roots", self.config["roots"])]
            if not any(repo == root or root in repo.parents for root in hook_roots):
                continue
            try:
                custom = command([self.git, "-C", str(repo), "config", "--get", "core.hooksPath"])
                if custom.returncode not in (0, 1):
                    raise RuntimeError(custom.stderr.strip())
                if custom.stdout.strip():
                    self.record(repo, "hook_deferred", "Existing core.hooksPath; explicit composition needed")
                    continue
                location = command([self.git, "-C", str(repo), "rev-parse",
                                    "--path-format=absolute", "--git-common-dir"])
                if location.returncode:
                    raise RuntimeError(location.stderr.strip())
                hook = Path(location.stdout.strip()) / "hooks/post-checkout"
                hook.parent.mkdir(parents=True, exist_ok=True)
                if hook.is_symlink():
                    raise RuntimeError("Refusing to replace hook symlink")
                import shlex
                content = """#!/bin/sh
# asimov-managed-v1
old_status=0
if [ -x "$0.pre-asimov" ]; then
    "$0.pre-asimov" "$@" || old_status=$?
fi
root=$(git rev-parse --show-toplevel) || exit 1
""" + shlex.join([executable, "reconcile", "--config", str(config_path),
                 "--no-hooks", "--root"]) + ' "$root" --json\nnew_status=$?\n' + """
if [ "$new_status" -ne 0 ]; then exit "$new_status"; fi
exit "$old_status"
"""
                if hook.exists():
                    prior = hook.read_text()
                    if "# asimov-managed-v1" not in prior:
                        saved = hook.with_name("post-checkout.pre-asimov")
                        if saved.exists():
                            raise RuntimeError("Prior hook backup already exists; manual reconciliation needed")
                        hook.rename(saved)
                fd, temporary = tempfile.mkstemp(dir=hook.parent)
                with os.fdopen(fd, "w") as stream:
                    stream.write(content)
                os.chmod(temporary, 0o755)
                os.replace(temporary, hook)
                self.record(hook, "hook_installed")
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                self.record(repo, "failed", str(error))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-hooks", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--exec", dest="execute", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    config = json.loads(args.config.expanduser().read_text())
    state = Path(config.get("state_dir", str(Path.home() / ".local/state/asimov"))).expanduser()
    if state.is_symlink():
        raise RuntimeError("State directory must not be a symlink")
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    if state.stat().st_uid != os.getuid():
        raise RuntimeError("State directory is not owned by the current user")
    lock_path = state / "reconcile.lock"
    if lock_path.is_symlink():
        raise RuntimeError("Lock must not be a symlink")
    install_status = 0
    if args.execute:
        # Do not hold the lock while running arbitrary package manager hooks.
        install_status = subprocess.run(args.execute).returncode
        args.root = args.root or Path.cwd()
    with lock_path.open("a") as lock:
        deadline = time.monotonic() + 900
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Timed out waiting for another reconciliation")
                time.sleep(0.25)
        engine = Reconciler(config, args.dry_run, not args.no_hooks)
        roots = [args.root.expanduser().resolve()] if args.root else engine.roots
        for root in roots:
            if not any(root == base or base in root.parents for base in engine.roots):
                engine.record(root, "out_of_scope", "Outside configured scan roots")
                continue
            engine.scan(root)
        for item in config.get("cache_paths", []) if not args.root else []:
            path = Path(item).expanduser()
            if path.exists() and not path.is_symlink():
                engine.exclude(path)
        engine.install_hooks(args.config.expanduser().resolve())
        counts = dict(collections.Counter(e["state"] for e in engine.events))
        receipt = {"version": VERSION, "time": dt.datetime.now(dt.timezone.utc).isoformat(),
                   "dry_run": args.dry_run, "roots": list(map(str, roots)),
                   "counts": counts, "events": engine.events}
        if not args.dry_run:
            # Unique receipts preserve hook failures even after later successful runs.
            stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            atomic_json(state / "receipts" / (stamp + ".json"), receipt)
            atomic_json(state / "latest.json", receipt)
            if counts.get("failed") or counts.get("hook_deferred"):
                atomic_json(state / "last-failure.json", receipt)
            limit = max(10, int(config.get("receipt_limit", 192)))
            for expired in sorted((state / "receipts").glob("*.json"))[:-limit]:
                if not expired.is_symlink():
                    expired.unlink()
        if args.json:
            print(json.dumps(receipt))
        else:
            print(json.dumps(counts, sort_keys=True))
            for event in engine.events:
                if event["state"] in ("failed", "hook_deferred"):
                    print(event["path"] + ": " + event["reason"], file=sys.stderr)
        return install_status or (1 if counts.get("failed") or counts.get("hook_deferred") else 0)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
        print("asimov reconcile: " + str(exc), file=sys.stderr)
        sys.exit(1)
