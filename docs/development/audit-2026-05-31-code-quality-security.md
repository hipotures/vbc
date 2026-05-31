# VBC Code Quality, Architecture, and Security Audit

Date: 2026-05-31

Scope: current `/home/xai/DEV/vbc` checkout. This audit covers the local
single-user desktop/terminal deployment model, plus the optional read-only web
dashboard that can expose compression progress on a LAN.

This report is intentionally report-only. It does not include code fixes.

## Executive Summary

VBC is a mature local batch-processing tool with useful separation between
domain models, pipeline orchestration, infrastructure adapters, and UI state.
The most important security conclusion is that the default local threat model
does not support critical remote findings. The meaningful risks are local data
loss, long-running or resource-heavy media processing, disclosure of local
filenames/paths, and LAN visibility when the optional web dashboard is enabled.

The most important operational finding is that `uv run pytest` is not a safe
routine command in this checkout because real video fixtures are present under
`tests/data` and the slow real-file tests are not excluded by default. Routine
verification should use explicit safe subsets.

Validated high-priority remediation themes:

- Make web dashboard exposure explicit before binding to all interfaces.
- Fix the undeclared `jinja2` runtime dependency for `--web`.
- Add normal CI coverage for unit and safe integration tests.
- Prevent accidental real-file compression from the default test command.
- Reduce or better defend large orchestration/UI hotspots over time.
- Clarify docs where current implementation differs from Clean Architecture
  claims and testing reality.

## Threat Model

Primary deployment:

- One user runs VBC locally with that user's OS privileges.
- The user provides input, output, and errors directories.
- VBC launches trusted local binaries: `ffmpeg`, `ffprobe`, `exiftool`, and
  optionally `nvtop`.
- The optional web dashboard is disabled by default, but can be enabled by CLI
  or config.

Realistic attackers and failure actors:

- A malicious or malformed video file processed by external media parsers.
- A local process/user able to write into scanned input, output, or config
  directories.
- A same-LAN user when `--web` or `web_server.enabled` exposes the dashboard.
- Another local OS user who can read world-readable logs or cwd artifacts.

Security invariants that matter:

- User media must not be moved, deleted, overwritten, or repaired unexpectedly.
- Local config files must not become code execution or shell injection.
- LAN observers should not see local filenames, errors, queue contents, or GPU
  telemetry unless the user intentionally exposes them.
- External tool failures must be visible and bounded.
- `conf/vbc.yaml` must remain untracked; it was verified untracked and ignored.

## Security Findings

### SEC-1: Web Dashboard Binds All Interfaces Without Auth When Enabled

Severity: Low by default, Medium when enabled on an untrusted LAN.

Affected code:

- `vbc/main.py:105` exposes `--web`.
- `vbc/config/models.py:400` has `web_server.enabled = False`.
- `vbc/config/models.py:402` defaults `web_server.host` to `0.0.0.0`.
- `vbc/infrastructure/web_server.py:564` routes plain `GET` requests without
  authentication.
- `vbc/infrastructure/web/templates/active_jobs.html:13`,
  `activity.html:25`, `queue.html:11`, and `gpu.html:7` render filenames,
  errors, queue state, and GPU telemetry.

Validation:

- The endpoint is opt-in, so this is not internet-exposed in the default
  configuration.
- When enabled with the default host, the dashboard listens on all interfaces.
- No auth token, cookie, session, origin, or host check was found.
- Response headers are minimal: content type, content length, and no-cache.
- Static path traversal is guarded and Jinja autoescape is enabled, so this is
  not currently a path traversal or template XSS finding.

Impact:

- Same-LAN users can observe local filenames, active work, error text, queue
  metadata, and GPU telemetry.
- No state-changing API was found, so this is information disclosure and
  local-observability exposure, not remote code execution.

Recommended remediation:

- Prefer `127.0.0.1` as the default bind address.
- If LAN access is intended, require an explicit config value and print a clear
  exposure warning.
- Consider optional token auth for LAN use.
- Add basic hardening headers and either vendor dashboard assets or use
  CSP/SRI for CDN assets.

### SEC-2: Local `VBC.YAML` Can Influence FFmpeg Encoder Arguments

Severity: Low in trusted single-user folders. Medium operational risk in shared
or synced input trees.

Affected code:

- `vbc/config/local_registry.py:105` scans input roots for local config.
- `vbc/config/local_registry.py:130` recognizes `VBC.YAML`.
- `vbc/config/overrides.py:136` uses `yaml.safe_load`.
- `vbc/config/overrides.py:17` allows `gpu_encoder` and `cpu_encoder`.
- `vbc/config/overrides.py:241` passes allowed encoder sections into overrides.
- `vbc/infrastructure/ffmpeg.py:33` tokenizes configured args with
  `shlex.split`.
- `vbc/infrastructure/ffmpeg.py:319` inserts encoder tokens into the FFmpeg
  argv list.

Validation:

- YAML object execution is not present because `safe_load` is used.
- Shell injection is not supported by the current command shape because FFmpeg
  is invoked with argv lists, not `shell=True`.
- The remaining trust issue is semantic: a local config file in a scanned tree
  can alter FFmpeg flags and processing decisions.

Impact:

- In a normal owner-controlled media folder this is expected behavior.
- In a shared folder, a local writer could alter output format, codec flags, or
  processing behavior for files below that directory.

Recommended remediation:

- Document local `VBC.YAML` as trusted input.
- For untrusted/shared folders, add an opt-in flag before honoring local
  encoder argument overrides.
- Consider restricting local overrides to quality/rate/filter decisions and
  excluding raw encoder args by default.

### SEC-3: Logs May Disclose Local Filenames, Paths, and Debug Commands

Severity: Low.

Affected code:

- `vbc/config/models.py:242` defaults the log path to
  `/tmp/vbc/compression.log`.
- `vbc/infrastructure/logging.py:31` creates a `FileHandler` without explicit
  restrictive chmod.
- `vbc/main.py:388` logs input folders.
- `vbc/infrastructure/ffmpeg.py:427` logs full FFmpeg command lines in debug.
- `vbc/pipeline/orchestrator.py:1006` logs ExifTool stderr/stdout on failures.
- `vbc/main.py:815` appends fatal tracebacks to `error.log`.

Validation:

- No application secrets were found in the app code path.
- The primary exposure is local metadata: paths, filenames, external-tool
  errors, and possibly command-line details.

Impact:

- Low for a single-user workstation.
- More relevant on shared Unix machines, synced project directories, or when
  log files are collected externally.

Recommended remediation:

- Document logs as sensitive local artifacts.
- Prefer a user-private log directory or explicitly set file mode for the
  default log path.
- Keep debug command logging opt-in.

## Suppressed Security Candidates

The following candidates were checked and are not reportable security findings
for the stated threat model:

- Shell injection: no `shell=True` or `os.system` was found in `vbc` or
  `scripts`; external tools use argv lists.
- YAML object execution: global and local YAML loading uses `yaml.safe_load`.
- Static file traversal: the web server resolves requested static paths and
  checks containment before reading.
- Template XSS: Jinja autoescape is enabled and no `|safe` / `Markup` bypass
  was found in the dashboard templates.
- File move/delete/temp cleanup as remote security issues: these are local CLI
  side effects rooted in configured input/output/error directories. They remain
  important data-loss risks, but not remote vulnerabilities in the single-user
  model.

## Architecture and Code Quality Findings

### ARCH-1: Clean Architecture Claims Are Stronger Than Current Boundaries

Severity: Medium.

Evidence:

- `docs/architecture/overview.md:7` claims Clean Architecture.
- `vbc/pipeline/orchestrator.py:42` imports concrete infrastructure adapters.
- `vbc/config/overrides.py:10` imports from `vbc.infrastructure.ffmpeg`.
- `vbc/infrastructure/gpu_monitor.py:10` imports and mutates `UIState`.
- `vbc/domain/events.py:156` contains UI-specific Dirs-tab input events.
- `tests/unit/test_architecture_boundaries.py:5` only checks that pipeline does
  not import UI directly.

Impact:

- The current design is workable for a local app, but the docs overstate the
  enforcement level.
- Boundary drift makes future refactors harder because concrete dependencies
  cross layers outside the composition root.

Recommended remediation:

- Update architecture docs to describe the current pragmatic boundaries.
- Expand boundary tests if strict Clean Architecture remains a goal.
- Keep future feature work from adding more concrete cross-layer imports.

### ARCH-2: Large Hotspots Concentrate Too Many Responsibilities

Severity: Medium.

Evidence:

- `vbc/pipeline/orchestrator.py` is 2071 LOC.
- `vbc/ui/modern_overlays.py` is 1451 LOC.
- `vbc/ui/dashboard.py` is 1430 LOC.
- `vbc/main.py` is 822 LOC.
- `docs/architecture/overview.md:84` still describes the orchestrator as
  "792 LOC".
- `vbc/pipeline/orchestrator.py` owns discovery, queueing, color fix remux,
  metadata copy, verification, error markers, file move/delete behavior,
  fallback, wait/restart, and refresh loops.

Impact:

- Changes to one behavior can accidentally affect unrelated processing paths.
- Review and test targeting are harder because one class owns many lifecycle
  concerns.

Recommended remediation:

- Prefer extraction only around real seams already visible in tests:
  verification/tagging, discovery/error-marker accounting, and completed-file
  move behavior.
- Do not start with a broad rewrite. Add narrow tests before each extraction.

### ARCH-3: EventBus Is Synchronous and Fragile Under Threaded Publishers

Severity: Medium.

Evidence:

- `vbc/infrastructure/event_bus.py:18` stores subscribers without locking.
- `vbc/infrastructure/event_bus.py:22` publishes synchronously.
- `vbc/infrastructure/ffmpeg.py:523` publishes progress from processing paths.
- `tests/unit/test_event_bus.py:8` covers happy-path publishing only.
- `docs/architecture/events.md:629` already recommends try/except around
  publish handlers.

Impact:

- A slow UI handler can delay a publisher.
- A handler exception can propagate into pipeline or subprocess control flow.
- Subscriber mutation during publish is not defended.

Recommended remediation:

- Decide whether synchronous propagation is intentional.
- If not, snapshot subscribers under a lock and isolate handler exceptions.
- Add tests for handler exceptions and concurrent subscribe/publish behavior.

### ARCH-4: CPU Fallback Can Drop a Job From Active UI State

Severity: Medium.

Evidence:

- `vbc/infrastructure/ffmpeg.py:551` publishes
  `HardwareCapabilityExceeded`.
- `vbc/ui/manager.py:285` removes that job from active jobs.
- `vbc/pipeline/orchestrator.py:1571` enters CPU fallback retry.
- `vbc/pipeline/orchestrator.py:1583` retries without publishing a fresh
  `JobStarted`.

Impact:

- When GPU fallback happens, the CPU retry may disappear from active UI state
  even though processing continues.
- This is not data corruption, but it weakens runtime observability during an
  important recovery path.

Recommended remediation:

- Publish a new `JobStarted` or dedicated fallback event before CPU retry.
- Add a focused test that simulates HW-cap failure and verifies UI active job
  state during fallback.

## Testing, CI, and Documentation Findings

### TEST-1: Full `uv run pytest` Is Unsafe in This Checkout

Severity: High operational risk.

Evidence:

- `pyproject.toml:38` sets `testpaths = ["tests"]`.
- `pyproject.toml:50` registers `slow` but does not exclude it by default.
- `tests/conftest.py:162` defines `real_test_videos`.
- `tests/conftest.py:209` modifies copied fixtures with `exiftool`.
- `tests/conftest.py:223` only moves real-file tests to the end of collection.
- `tests/integration/test_real_files_compression.py:17` is marked slow and
  integration, then runs the real compression path.
- This checkout contains `tests/data` at 632M and `tests/data_out` at 60M.

Impact:

- A routine full test command can run real video compression for a long time.
- This already affected the audit attempt and should be treated as a workflow
  hazard.

Recommended remediation:

- Make the default suite exclude `slow`, or require an explicit environment
  variable/marker for real-file tests.
- Document safe commands prominently.
- Consider moving real-file tests behind a separate tox/nox/CI job or script.

### TEST-2: CI Only Protects Documentation Sync and Build

Severity: High for regression prevention.

Evidence:

- `.github/workflows/deploy.yml:3` only triggers on `push` to `main` and
  `workflow_dispatch`.
- `.github/workflows/deploy.yml:35` installs dependencies.
- `.github/workflows/deploy.yml:48` runs only `tests/test_docs_sync.py`.
- `.github/workflows/deploy.yml:51` runs `mkdocs build`.

Impact:

- Runtime, pipeline, UI, config, and safe integration regressions are not
  protected by GitHub Actions.

Recommended remediation:

- Add a pull-request CI workflow for:
  - `uv run pytest tests/unit/ -q`
  - selected safe integration tests, excluding `test_real_files*`
  - `uv run pytest tests/test_docs_sync.py -q`
  - `uv run mkdocs build`

### TEST-3: `--web` Has an Undeclared Runtime Dependency

Severity: Medium.

Evidence:

- `vbc/main.py:105` exposes `--web`.
- `vbc/main.py:624` imports/starts `VBCWebServer` when enabled.
- `vbc/infrastructure/web_server.py:6` claims no new dependencies.
- `vbc/infrastructure/web_server.py:25` imports `jinja2`.
- `pyproject.toml:11` runtime dependencies do not include `jinja2`.
- `uv.lock:241` contains `jinja2`, apparently transitively through docs
  dependencies.

Impact:

- A lean install that only installs runtime dependencies can fail when `--web`
  is enabled.

Recommended remediation:

- Add `jinja2` to runtime dependencies, or remove the runtime dependency by
  replacing template rendering with stdlib rendering.
- Update the web server module docstring.

### TEST-4: Lockfile and Reproducibility Docs Drift

Severity: Medium.

Evidence:

- `README.md:320` and `docs/getting-started/installation.md:46` recommend
  `uv sync --frozen`.
- `uv.lock` exists locally but `git ls-files uv.lock` returned no tracked file.
- `git check-ignore -v uv.lock` reports `.gitignore:2:*`.
- `.github/workflows/deploy.yml:10` watches `uv.lock`, but the workflow uses
  non-frozen `uv sync`.

Impact:

- Installation docs imply reproducibility that the repository does not provide.
- CI path filters mention a lockfile that is not currently tracked.

Recommended remediation:

- Either track `uv.lock` and use frozen sync where appropriate, or remove
  frozen-lock wording and lockfile path triggers.

### TEST-5: Test Marker Documentation Does Not Match Current Tests

Severity: Medium.

Evidence:

- `docs/development/testing.md:331` documents `@pytest.mark.unit`.
- `docs/development/testing.md:361` recommends `uv run pytest -m unit`.
- Actual marker usage is concentrated in slow real-file integration tests; no
  `@pytest.mark.unit` tests were found.

Impact:

- Developers can run a command that selects little or nothing useful.

Recommended remediation:

- Either add unit/integration markers consistently, or document path-based
  commands as the supported workflow.

### TEST-6: Scratch Scripts Named `test1.sh` / `test2.sh` Are Tracked

Severity: Medium.

Evidence:

- `scripts/test1.sh:6` and `scripts/test2.sh:6` use hard-coded private
  `/arch03/V/...mov` inputs.
- Both call `ffmpeg -y` and write `proxy.mp4` / `proxy-gpu.mp4`.

Impact:

- The names look like normal test scripts but execute real FFmpeg jobs.

Recommended remediation:

- Rename as manual experiments and document them, or remove them from tracked
  repo if they are personal scratch files.

## Verified Strengths

- `conf/vbc.yaml` is not tracked and is ignored by `.gitignore`.
- External command execution in `vbc` and tracked scripts uses argv lists; no
  `shell=True` / `os.system` was found.
- YAML loading uses `yaml.safe_load`.
- Static serving has a path traversal containment check.
- Jinja autoescape is enabled for dashboard templates.
- Pydantic config validation covers many dangerous user-input shapes.
- Pipeline does not directly import `vbc.ui`, and there is a boundary test for
  that rule.
- Submit-on-demand limits queued futures instead of submitting every discovered
  file at once.
- The focused unit and safe integration suites are fast enough for routine use.

## Verification Evidence

Commands run during this audit:

```bash
git status --short
git ls-files conf/vbc.yaml
git check-ignore -v conf/vbc.yaml
uv run pytest tests/test_docs_sync.py -q
uv run pytest tests/unit/ -q
uv run pytest tests/integration/test_metadata_copy.py tests/integration/test_skipping.py tests/integration/test_orchestrator.py tests/integration/test_hw_cap.py tests/integration/test_error_markers.py tests/integration/test_concurrency.py tests/integration/test_color_fix.py tests/integration/test_advanced_errors.py -q
uv run mkdocs build
```

Observed results before this report was written:

- `git status --short`: clean.
- `git ls-files conf/vbc.yaml`: no output.
- `git check-ignore -v conf/vbc.yaml`: `.gitignore:21:conf/vbc.yaml`.
- `uv run pytest tests/test_docs_sync.py -q`: `10 passed in 0.02s`.
- `uv run pytest tests/unit/ -q`: `281 passed in 1.80s`.
- Safe selected integration subset: `26 passed in 22.71s`.
- `uv run mkdocs build`: completed; it reported existing nav omissions for
  `DOCUMENTATION_CHANGELOG.md` and `development/config_vs_cli_analysis.md`, plus
  mkdocstrings/griffe warnings unrelated to this report.

Commands intentionally not run:

```bash
uv run pytest
uv run pytest tests/integration/test_real_files*.py
uv run pytest --cov=vbc --cov-report=term-missing
```

Reason: the current checkout contains 632M of real video fixtures and the
default full suite can run real compression for a long time.

## Prioritized Remediation Backlog

1. Gate real-file compression tests behind explicit opt-in and update the test
   docs to make safe commands the default.
2. Add CI for unit tests, docs-sync, docs build, and safe integration tests on
   pull requests.
3. Fix the `--web` runtime dependency by declaring `jinja2` or removing the
   dependency.
4. Change web dashboard default host to `127.0.0.1`, or add an explicit LAN
   exposure warning and optional token auth.
5. Decide whether `uv.lock` is meant to be tracked; align `.gitignore`, docs,
   and CI with that decision.
6. Add EventBus exception/concurrency tests, then harden `publish()` if the
   current synchronous behavior is not intentional.
7. Add a HW-cap CPU fallback UI regression test and publish a fallback-start
   event or equivalent state update.
8. Update architecture docs to describe the current pragmatic boundaries and
   current file sizes.
9. Review `scripts/test1.sh` and `scripts/test2.sh`; rename, document, or
   remove them.
10. Plan narrow extractions from `Orchestrator` only where tests can pin current
    behavior first.
