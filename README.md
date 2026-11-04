# Agensfield Asimov

A maintained Asimov fork for frequently recreated development worktrees.
MIT licensed; based on AsimovMac/asimov (see existing license and history).

Install: `brew install agensfield/tap/asimov`.

The default entry point is managed reconciliation, also spelled `asimov reconcile`. It uses Python's standard
library for deterministic discovery, live Time Machine exclusion checks,
tracked-file and protected-path guards, serialized runs, and verified receipts.
Upstream's legacy scan commands require the explicit legacy subcommand and are not
used by this fork's managed service.

Copy and review config/managed.example.json as ~/.config/asimov/managed.json.
Preview: `asimov reconcile --dry-run --json`.
Apply: `asimov reconcile --json`.
Start the 15-minute fallback: `brew services start agensfield/tap/asimov`.

A targeted run is `asimov reconcile --root /absolute/project --json`.
Wrap installs with `asimov reconcile --root "$PWD" --exec bun install` (or
another package manager). The install exit status is preserved; reconciliation
still runs after a failed install. Direct installs are covered by the fallback.

With install_hooks enabled, reconciliation installs an idempotent per-repository
post-checkout dispatcher. Existing executable hooks are retained as
post-checkout.pre-asimov and run first. Configured core.hooksPath is reported
as hook_deferred and must be explicitly composed. No global hooks setting is
overwritten. Ramiz and native Git can share this integration point.

Receipts: ~/.local/state/asimov/receipts and latest.json. Failures return nonzero.
Notifications point to durable last-failure.json because later scoped successes
can replace latest.json. Failed events also reach the service stderr log in JSON
mode.
The service notifies only on failure. Git/permission errors are never interpreted
as evidence that content is disposable. Symlink trees, tracked content and
protected names are kept. No Spotlight index or successful-path cache is trusted
for discovery. Sticky markers alone are insufficient when Time Machine cannot
discover them through Spotlight, notably inside hidden worktree directories.
Directory attributes are set even where a fixed-path exclusion already applies,
allowing the Ramiz inheritance patch to propagate actual donor intent.

Use `cache_files` for an exact allowlist of individually reviewed rebuildable
files, such as a derived database and its sidecars. Existing regular files are
marked and verified during full reconciliation; missing files are skipped and
recreated files are checked again on the next sweep. `cache_paths` remains
directory-only. Protected paths and symlinks cannot be overridden by either
allowlist. Keep original histories and unrelated databases backed up.

### Native fixed-path registration

Set `fixed_path_exclusions: true` in the reviewed managed config, then use the
signed background setup below. It installs both the app and privilege bridge.

The existing user service keeps both portable item markers and native
`tmutil addexclusion -p` entries. Native path entries bypass sticky-marker
discovery through Spotlight and survive deletion/recreation at the same path.
Receipts distinguish `fixed_registered` and `fixed_verified`; verification reads
the native `SkipPaths` preferences through `defaults export`, not just
`tmutil isexcluded` or a potentially delayed on-disk preferences plist.

The root-owned helper accepts only approved dependency/sentinel pairs beneath
installed roots and exact reviewed cache paths/files. It cannot exclude a whole
worktree, run Git, change backup schedules, or execute configurable commands.

### Signed background identity

For automatic native path maintenance, install the native Homebrew launcher and
prepare an Asimov-specific, Developer ID-signed background app:

```sh
asimov setup-background --identity "Developer ID Application: Your Name (TEAMID)"
```

Signing runs as your normal user using your existing Keychain identity; the key
is never exported. The explicit sudo step installs the signed app in
`/Applications/Asimov Background.app` and the same restricted settings helper.
The installer verifies the complete signature and refuses a replacement with a
different designated signing requirement. A stable bundle identifier and
Developer ID requirement let macOS bind permissions to Asimov across updates.
The app uses the stable installed CLI entry point, so routine engine upgrades do
not require rebuilding the signed launcher. Homebrew builds it locally; signing
keys and prebuilt personally signed binaries are not uploaded to the repository.

The one Homebrew service launches this app when installed, with the same
900-second schedule. Its native main executable stays alive as the responsible
parent of the existing reconciler. The app forwards no arbitrary commands and
refuses to run as root. Without the installed app, Homebrew uses its native
launcher directly; granting an interpreter permission is not the managed setup.

Before enabling the service, verify launchd attribution with the app's
`--check-identity` mode. This fixed read-only probe uses the restricted helper to
open the system's protected TCC database and close it without reading content. It grants no
permission and changes no database. Enable **Asimov Background** manually in
System Settings > Privacy & Security > Full Disk Access, then restart the
Homebrew service. FDA has no ordinary Allow prompt. Verify a real background
native-list write after the grant; interactive Terminal/Codex success is not
background acceptance. The helper never changes TCC grants or its database.

The sudo rule permits only this standalone helper through Apple's isolated
Python interpreter. Scanning, Git checks, and the existing service remain under
the normal user; no daemon or repository hooks are added. Reinstall the helper
after changing its root-owned policy; a mismatch is reported as a failure.
Policy-only updates do not require re-signing the app:

```sh
sudo asimov install-path-helper --user "$USER" --config "$HOME/.config/asimov/managed.json"
```

Only entries created by this helper are retired during complete reconciliation
of their scope. Pre-existing manual exclusions are never adopted or removed.
Incomplete scans do not retire entries. Explicit cache allowlists remain
registered even while temporarily absent. Disabling this option does not remove
existing entries or markers. This does not reclaim local APFS snapshot space,
remove historical backup data, or change a run's already-captured policy.

Run as the normal user, with Full Disk Access where required. Do not run the
service as root. Existing historical backup data is unaffected and may still
require pruning. A post-checkout/install hook leaves a timing window before it
runs; periodic reconciliation is a fallback, not a zero-exposure guarantee.

Validation:
`python3 -m unittest discover -s tests -p 'test_*.py' -v`
and `make check` for upstream compatibility.

Stop with `brew services stop agensfield/tap/asimov`. Disabling the service
does not remove exclusion attributes. Inspect receipts before removing selected
attributes with tmutil removeexclusion. Restore a saved previous hook only after
checking its dispatcher and .pre-asimov backup.


CI exercises macOS 26 and macOS 27 preview (xcode-27), with both system and
Homebrew Bash. Releases run on macOS 26. The Homebrew tap is the supported
installation and scheduling path; upstream source/curl installers are disabled
in this fork to prevent accidentally scheduling the legacy daily scanner.
