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
- PyInstaller Windows build completed successfully. Build outputs are ignored.

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
