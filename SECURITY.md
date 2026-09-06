# Public repository policy

Only code, synthetic tests, build configuration and documentation belong in Git.
Never include real Client IDs, secrets, tokens, account identifiers, API payloads,
chats, reviews, exports, media, profiles or user-data logs in any public file.

`.gitignore` uses a source allowlist. Local pre-commit and pre-push hooks scan
staged files and every ancestor of the pushed commit, including file types,
symlinks and common credential patterns. CI repeats the history check.
Enable hooks in other clones with `git config core.hooksPath .githooks`.

These checks are defense in depth, not a mathematical guarantee: Git allows
forced additions and bypassed hooks, and no pattern scanner recognizes all
personal data embedded in code. Review all staged changes before publishing.

UI binds only to 127.0.0.1. Do not expose it through a proxy or tunnel. Profiles
live in the OS user configuration directory; secrets use OS keyring with no
plaintext fallback. Export archives are private, unencrypted user data and can
contain temporary signed media URLs. Keep them outside the checkout.

OAuth response bodies are never archived. Approved data GET responses and the two
read-only Statistics API POST responses, including errors,
are preserved without JSON rewriting; response cookies are redacted in metadata.
The API client accepts only explicitly approved endpoints. Data POST is restricted
to `/stats/v2/accounts/{user_id}/items` and `/spendings`; other POST paths are
rejected before network access. Media use HTTPS Avito domain boundaries and checked redirects;
external URLs remain in RAW JSON and are not downloaded.


Recovery databases, checkpoint backups, per-voice indexes and private audit reports
are sensitive archive data and must remain outside Git. Resume checks account identity
before migration and takes an OS file lock. Opaque chat IDs are mapped to portable
filenames separately from URL validation; special IDs never become path components.
