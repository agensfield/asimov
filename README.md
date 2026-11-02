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
The service notifies only on failure. Git/permission errors are never interpreted
as evidence that content is disposable. Symlink trees, tracked content and
protected names are kept. No Spotlight index or successful-path cache is trusted.
Directory attributes are set even where a fixed-path exclusion already applies,
allowing the Ramiz inheritance patch to propagate actual donor intent.

Run as the normal user, with Full Disk Access where required. Do not run the
service as root. Existing historical backup data is unaffected and may still
require pruning. A post-checkout/install hook leaves a timing window before it
runs; periodic reconciliation is a fallback, not a zero-exposure guarantee.

Validation:
`python3 -m unittest discover -s tests -p 'test_reconcile.py' -v`
and `make check` for upstream compatibility.

Stop with `brew services stop agensfield/tap/asimov`. Disabling the service
does not remove exclusion attributes. Inspect receipts before removing selected
attributes with tmutil removeexclusion. Restore a saved previous hook only after
checking its dispatcher and .pre-asimov backup.


CI exercises macOS 26 and macOS 27 preview (xcode-27), with both system and
Homebrew Bash. Releases run on macOS 26. The Homebrew tap is the supported
installation and scheduling path; upstream source/curl installers are disabled
in this fork to prevent accidentally scheduling the legacy daily scanner.
