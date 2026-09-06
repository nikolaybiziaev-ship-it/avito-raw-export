# Technical audit — 2026-09-06

This is a standalone open-source exporter, unrelated to AI Sales Agent.
No real Avito credentials or real account data were used in this audit.

## Corrections

- Fixed module/Windows launchers and the frozen executable entrypoint; the UI
  explicitly binds to loopback and builds separate forms for browser clients.
- Replaced vulnerable NiceGUI 2.x with NiceGUI 3.16+, supporting the installed
  Python 3.14; upgraded pytest and pip to patched versions.
- Enforced a data endpoint GET allowlist. OAuth token POST is the only POST;
  redirects cannot forward OAuth credentials. OAuth bodies never reach export
  callbacks. Added bounded network/server/rate-limit retries and token refresh.
- Preserved every data API response before parsing/raising, including failed
  attempts, alongside byte counts, SHA-256 and redacted metadata.
- Added unique export directories, atomic manifest replacement and containment
  checks against path traversal. Media also have source/hash metadata.
- Continued pagination after short pages, advancing Messenger offsets by actual
  row count. Fixed item pagination fallback, added repeated-page detection and
  explicit partial results on API ceilings, malformed responses or missing voices.
- Repeated item/chat discovery until no new IDs remain, including item IDs found
  only in full chat objects. Final indexes reflect the expanded discovery set.
- Added completed/partial/failed status and UI warnings for incomplete exports.
- Replaced substring media host checks with HTTPS domain-boundary checks and
  validated every redirect. Collected nested media and account media.
- Saved profile metadata only after keyring succeeds; invalid existing profile
  JSON is reported rather than silently overwritten. Credentials are captured
  before background work; failed secret loading clears the previous value.
- Added default-deny Git ignores, source/history guards, enabled local commit/push
  hooks, and CI checks for Windows/Linux with Python 3.11/3.14.

## Validation performed locally

- Windows, Python 3.14.3; virtual environment with runtime, test and build tools.
- 42 automated tests passed: full synthetic export, pagination, error/RAW
  preservation, retries, OAuth refresh, endpoint restrictions, malicious URLs,
  traversal, hashes, profile failure handling, entrypoint and actual Git ignores.
- Ruff checks passed; `pip check` reported no broken requirements.
- `pip-audit` reported no known vulnerabilities after dependency upgrades.
  The local unpublished project is reviewed as source, not through a PyPI advisory.
- Gitleaks scan of staged public files found no leaks; public-code guard passed.
- Windows Credential Manager write/read/delete succeeded with a disposable
  synthetic entry. No real profile was created or edited.
- UI rendered at localhost; empty-key validation worked. A separate temporary
  UI test server completed all export stages with synthetic responses and zero
  errors; its private test artifacts are ignored and not published.
- PyInstaller Windows build completed successfully; its EXE rendered the UI.
  Build outputs are ignored.
- The initial Linux CI run passed 42 tests but found an outdated preinstalled
  setuptools. Installers and CI now upgrade build tools; the build requirement
  is setuptools 83 or newer.

## Limits requiring a real account

No live Avito API export has been performed. Account permissions, available
statuses, actual response contracts, media hosts and oldest accessible history
must be confirmed on first connection. Official developer documentation could
not be retrieved from this environment during the audit; existing endpoint
assumptions are covered by mocks, not presented as live-verified contracts.

Messenger retains the project's offset ceiling of 1000. Reaching it produces
an explicit partial result. Offset pagination is not a transactionally consistent
snapshot if the account changes during export. A successful export cannot prove
that Avito exposed every historical entity. Unsupported media hosts are retained
as RAW links and refused for download; large downloads are buffered in memory.

Archives contain private, unencrypted data and may include temporary signed media
URLs. Never publish them. Git ignores and scanners can be intentionally bypassed
and cannot identify every possible personal value embedded in source; see
[SECURITY.md](SECURITY.md) for the publication policy.


# v0.2 recovery audit

The v0.2 investigation used a private, local v0.1 archive. No real IDs, messages,
reviews, media, credentials or private report are included in this repository.
Original v0.1 audit statements above describe that earlier validation only.

Changes: independent chat ID validation/URL encoding/file mapping including tilde;
foreign-item 422 classification and persistent suppression; Avito rate-limit header;
singleton voice lookup; validated request replay; private SQLite checkpoints;
atomic manifest/index replacement with bounded Windows sharing-lock recovery;
streamed media and per-file resume; archive locking and account identity check;
separate error/limitation counters and UI resume/stop controls.

Validation includes the previous suite plus interrupted-message/media resume,
legacy running-archive import, no repeated saved downloads, anonymous live ID shapes,
malicious identifiers, API 404/422/429, partial voice responses, streaming, atomic
replacement failure, wrong-account rejection and archive locking. A browser test
completed interrupted → resume → completed using a synthetic API. Private RAW and
media were hash-checked on a separate local copy. Real-network completion of that
archive is left to the user's resume action; no new live responses are claimed.

Known limits: replay rebuilds local indexes before reaching missing network work;
offset pages are not a consistent live snapshot; public API ceilings remain explicit
limitations. An abruptly killed process may leave `running`, which is resumable.
The streaming implementation supersedes the v0.1 memory-buffering limitation above.
