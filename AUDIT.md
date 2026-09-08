# Technical Audit

This document summarizes the public technical checks expected for Avito Raw
Export. It contains no credentials, account identifiers, exported conversations,
private reports, or real Avito payloads.

## Read-only Boundary

- OAuth token exchange is the only authentication request.
- Data access is limited by an explicit endpoint allowlist.
- Messenger mutation paths are blocked, including send, read, delete, blacklist,
  webhook, and listing update actions.
- Statistics are limited to the two read-only Avito Statistics API v2 POST
  resources for item/account indicators and spendings.
- OAuth response bodies and access tokens are never passed to archive writers.

## Archive Integrity

- RAW API responses are saved before parsing so failed and malformed responses
  remain inspectable.
- Metadata records HTTP method, URL, parameters or request body, status code,
  redacted headers, byte count, and SHA-256.
- Archive writes use containment checks and atomic replacement for generated JSON.
- Export directories are unique.
- Recovery state is stored in `index/recovery.sqlite3` inside the private archive.
- Resume checks account identity before continuing an existing archive.

## Pagination And Recovery

- Item, review, chat, message, and statistics pagination are covered by synthetic
  tests.
- Message pages are merged by offset and repeated-page detection protects against
  loops.
- `analysis_ready/` deduplicates repeated `message_id` values and sorts messages
  by timestamp.
- Interrupted exports can resume from recorded checkpoints.
- API ceilings, malformed responses, and unavailable optional data produce
  explicit partial results instead of silently succeeding.

## AI-ready Corpus

- `analysis_ready/` is generated locally from an existing archive and makes no
  Avito API calls.
- `conversations.jsonl` is compact and intended for LLM or analytics pipelines.
- Full `conversations/conversation_<safe_id>.json` files remain available for
  audit.
- Conversation types are separated as `customer_item`, `avito_system`,
  `avito_support`, and `other`.
- System messages, including `author_id=0`, are not labeled as customer messages.

## Security Controls

- `.gitignore` denies repository content by default and explicitly ignores
  exports, generated analysis data, logs, SQLite databases, media, JSONL, CSV,
  archives, profiles, and common credential files.
- `scripts/check_public.py` allows only source, tests, scripts, workflows,
  selected documentation, and build launchers.
- The public guard rejects oversized or binary files, symlinks/submodules,
  unexpected paths, and common credential patterns.
- Pre-commit and pre-push hooks run the public guard.
- CI repeats the public guard against the checked commit history.

## Validation

Before publication, run:

```bash
python -m ruff check src tests scripts
python -m pytest -q
python scripts/check_public.py HEAD
python -m pip check
```

CI runs the same core checks on Linux and Windows across supported Python
versions. Optional dependency vulnerability checks run through `pip-audit`.

## Known Limits

- Avito controls which historical data and fields are available to an account.
- Offset-based API pagination is not a transactionally consistent snapshot.
- A successful export cannot prove that Avito exposed every historical entity.
- Export archives are private, unencrypted local data and can contain personal
  or business information.
