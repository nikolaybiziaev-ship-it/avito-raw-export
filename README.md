# Avito Raw Export

Avito Raw Export is a local, open-source, read-only tool for exporting available
Avito Business API chat and message history. It preserves original API responses
as a RAW archive and can build a compact AI-ready JSONL corpus for ChatGPT, Claude,
DeepSeek, local LLMs, or your own analytics.

The tool runs on your computer, stores credentials locally, and does not send,
mark as read, delete, or modify Avito data.

## Why Use It

- Keep a local copy of available Avito conversation history.
- Analyze real customer questions, objections, and sales patterns.
- Prepare chat history for LLM analysis without locking data to one SaaS product.
- Keep RAW API responses as the source of truth.
- Use compact JSONL for analysis and full JSON files for audit.
- Resume interrupted exports instead of starting over.

## What It Does

- Connects to Avito Business API with OAuth client credentials.
- Exports account metadata, listings needed for chat discovery, chats, full chat
  objects, and all available message pages.
- Preserves original RAW JSON responses and redacted request metadata.
- Builds `analysis_ready/` from an existing archive without new API requests.
- Separates conversations into `customer_item`, `avito_system`,
  `avito_support`, and `other`.
- Optionally exports reviews, historical statistics, Avito-hosted media, and
  voice files.
- Keeps exports, logs, profiles, tokens, and generated data outside Git.

## Quick Start On Windows

1. Install Python 3.11 or newer.
2. Download or clone this repository.
3. Double-click `run_windows.bat`.
4. Open `http://127.0.0.1:8765` if the browser does not open automatically.
5. Enter a profile name, Client ID, and Client Secret for your Avito Business API
   application.
6. Click `Проверить подключение`, then `Сохранить профиль`.
7. Choose an export folder outside the repository.
8. Leave `Чаты и сообщения` enabled and click `Выгрузить историю переписок`.
9. After export completion, click `Подготовить для анализа`.

Manual run from source:

```bash
python -m pip install -e .
python -m avito_raw_export
```

## Output

Each export creates a private archive directory:

```text
exports/
└── 2026-09-06_120000_export_a1b2c3d4e5f6/
    ├── manifest.json
    ├── raw/
    │   ├── requests/
    │   ├── account/
    │   ├── items/
    │   ├── chats/
    │   ├── messages/
    │   ├── ratings/
    │   └── statistics/
    ├── index/
    ├── media/
    ├── analysis_ready/
    └── logs/
```

`raw/` contains original API responses. Each saved response also has metadata
with HTTP method, URL, parameters or request body, status code, redacted headers,
size, and SHA-256. OAuth response bodies, access tokens, cookies, and Client
Secrets are not archived.

## AI-ready Export

```text
Avito Business API
→ RAW archive
→ analysis_ready
→ ChatGPT / Claude / DeepSeek / local LLM / custom analytics
```

`analysis_ready/` is generated locally from an existing archive. It does not make
new Avito API requests and does not modify `raw/`.

```text
analysis_ready/
├── corpus_manifest.json
├── items.json
├── conversations.jsonl
├── conversations_index.csv
├── conversations/
│   ├── conversation_<safe_id>.json
│   └── ...
├── human_readable/
│   ├── conversation_<safe_id>.txt
│   └── ...
└── README.txt
```

`conversations.jsonl` is the main compact LLM artifact. One JSON line equals one
complete conversation with listing context, period, counts, and ordered messages.
It avoids heavy RAW noise such as avatars, full public profiles, full chat
objects, preview image variants, and repeated content structures.

`conversations_index.csv` is for filtering the corpus before analysis. The
`conversation_type` column can be:

- `customer_item` — a customer conversation about a listing;
- `avito_system` — an Avito system conversation;
- `avito_support` — Avito support;
- `other` — anything that cannot be classified from structured fields.

For business analysis, start with `customer_item` rows. Full
`conversations/conversation_<safe_id>.json` files remain available for audit and
technical checks.

## Read-only And Safety

The API client only allows explicit read-only endpoints. Messenger write actions
such as sending messages, marking chats as read, deleting messages, blacklist
actions, webhooks, and listing updates are not implemented and are blocked by the
endpoint allowlist.

Profiles are local. Client Secrets are stored in the operating system keyring.
The UI binds to `127.0.0.1` only. Export archives can contain personal and
business data, so keep them outside the repository and do not publish them.

The repository uses a deny-by-default `.gitignore`, local Git hooks, and CI
checks to reject credentials, JSON/CSV exports, logs, SQLite databases, media,
archives, and unexpected public files.

## Limitations

- Avito API only returns data available to the authenticated application and
  account permissions.
- Historical chats and messages are limited by Messenger API pagination and
  availability.
- Exports are not transactional snapshots; Avito data can change during a run.
- Statistics API history is limited by Avito's documented retention windows.
- Some media URLs may be temporary or unavailable by the time they are downloaded.
- Disabled optional modules are not exported and do not affect export status.

## Optional Modules

The default workflow exports chat and message history. The UI can also enable:

- reviews and rating;
- historical item/account statistics and spendings;
- Avito-hosted images and other media;
- voice file link lookup and voice downloads.

Statistics use Avito Statistics API v2 read-only POST endpoints:

- `POST /stats/v2/accounts/{user_id}/items`;
- `POST /stats/v2/accounts/{user_id}/spendings`.

Statistics requests are rate-limited, checkpointed, and resumable. Successful
responses are saved in `raw/statistics/`; all attempts are preserved in
`raw/requests/`.

## Developer Notes

Install development dependencies and run checks:

```bash
python -m pip install -e .[dev]
python -m ruff check src tests scripts
python -m pytest -q
python scripts/check_public.py HEAD
```

Enable local publication hooks in a clone:

```bash
git config core.hooksPath .githooks
```

Windows build:

```bash
build_windows.bat
```

See [SECURITY.md](SECURITY.md) for the publication policy and [AUDIT.md](AUDIT.md)
for a neutral technical checklist.
