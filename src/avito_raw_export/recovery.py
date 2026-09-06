"""Durable local request/media journal, including non-destructive v1 import."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from .client import AvitoApiError, RawResponse
from .issues import classify, issue_key
from .store import atomic_json


def digest_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def request_key(url, params=None):
    parsed = urlsplit(url)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    if not query and params:
        query = list(params.items()) if isinstance(params, dict) else list(params)
    return json.dumps(
        [parsed.path, sorted((str(k), str(v)) for k, v in query)], ensure_ascii=True
    )


class Journal:
    def __init__(self, store):
        self.store = store
        self.importing = False
        self.db = sqlite3.connect(store.index / "recovery.sqlite3")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS requests (key TEXT PRIMARY KEY, path TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS media (key TEXT PRIMARY KEY, status TEXT, path TEXT, digest TEXT, bytes INTEGER, reason TEXT);
            CREATE TABLE IF NOT EXISTS issues (key TEXT PRIMARY KEY, category TEXT, reason TEXT, is_error INTEGER, active INTEGER, context TEXT);
            CREATE TABLE IF NOT EXISTS statistics_windows (
                key TEXT PRIMARY KEY, endpoint TEXT NOT NULL, date_from TEXT NOT NULL,
                date_to TEXT NOT NULL, state TEXT NOT NULL, rows INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL, error TEXT
            );
            CREATE TABLE IF NOT EXISTS statistics_records (
                source_endpoint TEXT NOT NULL, window_from TEXT NOT NULL,
                window_to TEXT NOT NULL, grouping_type TEXT NOT NULL,
                record_id TEXT NOT NULL, metric TEXT NOT NULL, value TEXT,
                fetched_at TEXT NOT NULL,
                PRIMARY KEY(source_endpoint, window_from, window_to, grouping_type, record_id, metric)
            );
        """)

    def close(self):
        self.db.close()

    def index_response(self, raw, path):
        if raw.status_code not in (200, 201, 204, 400, 403, 404, 410, 422):
            return
        if raw.status_code == 422:
            probe = AvitoApiError(
                "validation",
                status_code=422,
                body=raw.content.decode("utf-8", errors="replace"),
            )
            if classify("item-detail", probe)[1] != "foreign_item":
                return
        if raw.status_code < 300:
            try:
                raw.json()
            except (ValueError, UnicodeError):
                return
        self.db.execute(
            "INSERT OR REPLACE INTO requests VALUES (?,?)",
            (request_key(raw.url, raw.params), str(path.relative_to(self.store.root))),
        )
        if not self.importing:
            self.db.commit()

    def cached(self, url, params):
        row = self.db.execute(
            "SELECT path FROM requests WHERE key=?", (request_key(url, params),)
        ).fetchone()
        if not row:
            return None
        try:
            path = self.store._contained(self.store.root, row[0])
            meta = json.loads(path.with_suffix(path.suffix + ".meta.json").read_bytes())
            body = path.read_bytes()
            if (
                len(body) != meta["bytes"]
                or hashlib.sha256(body).hexdigest() != meta["sha256"]
            ):
                return None
            return RawResponse(
                meta["method"],
                meta["url"],
                meta.get("params"),
                meta["status_code"],
                meta["headers"],
                body,
            )
        except (OSError, ValueError, KeyError):
            return None

    def import_files(self, on_progress=None):
        self.importing = True
        # RAW stays untouched. Only private indexes are created/rebuilt.
        for count, meta_path in enumerate(
            sorted((self.store.raw / "requests").glob("*.meta.json")), 1
        ):
            if on_progress and count % 250 == 0:
                on_progress(f"Проверено RAW-ответов: {count}")
            path = meta_path.with_name(meta_path.name.removesuffix(".meta.json"))
            try:
                meta = json.loads(meta_path.read_bytes())
                body = path.read_bytes()
                if (
                    len(body) != meta["bytes"]
                    or hashlib.sha256(body).hexdigest() != meta["sha256"]
                ):
                    continue
                raw = RawResponse(
                    meta["method"],
                    meta["url"],
                    meta.get("params"),
                    meta["status_code"],
                    meta["headers"],
                    body,
                )
                self.index_response(raw, path)
            except (OSError, ValueError, KeyError):
                continue
        # Synthetic clients / interrupted older versions may have thematic RAW only.
        for meta_path in self.store.raw.rglob("*.meta.json"):
            if meta_path.parent.name == "requests":
                continue
            path = meta_path.with_name(meta_path.name.removesuffix(".meta.json"))
            try:
                meta = json.loads(meta_path.read_bytes())
                if self.cached(meta["url"], meta.get("params")) is None:
                    body = path.read_bytes()
                    if hashlib.sha256(body).hexdigest() == meta["sha256"]:
                        self.index_response(
                            RawResponse(
                                meta["method"],
                                meta["url"],
                                meta.get("params"),
                                meta["status_code"],
                                meta["headers"],
                                body,
                            ),
                            path,
                        )
            except (OSError, ValueError, KeyError):
                continue
        for meta_path in self.store.media.rglob("*.meta.json"):
            try:
                meta = json.loads(meta_path.read_bytes())
                path = meta_path.with_name(meta_path.name.removesuffix(".meta.json"))
                path = self.store._contained(
                    self.store.root, str(path.relative_to(self.store.root))
                )
                if (
                    path.stat().st_size != meta["bytes"]
                    or digest_file(path) != meta["sha256"]
                ):
                    continue
                self.media_done(
                    meta.get("key", meta["url"]), path, meta["sha256"], meta["bytes"]
                )
            except (OSError, ValueError, KeyError):
                continue
        # Associate durable voice IDs with v1 files despite expiring URLs.
        for p in (self.store.raw / "voice" / "links").glob("*.json"):
            if p.name.endswith(".meta.json"):
                continue
            try:
                payload = json.loads(p.read_bytes())
                for voice_id, value in (
                    payload.get("voices_urls") or payload.get("voicesUrls") or {}
                ).items():
                    url = value if isinstance(value, str) else value.get("url")
                    row = self.media_record(url)
                    if row and row[0] == "done":
                        self.db.execute(
                            "INSERT OR REPLACE INTO media VALUES (?,?,?,?,?,?)",
                            ("voice:" + voice_id, *row),
                        )
            except (OSError, ValueError, AttributeError):
                continue
        self.db.commit()
        self.importing = False

    def media_record(self, key):
        return self.db.execute(
            "SELECT status,path,digest,bytes,reason FROM media WHERE key=?", (key,)
        ).fetchone()

    def media_exists(self, key):
        row = self.media_record(key)
        if not row or row[0] != "done":
            return False
        try:
            p = self.store._contained(self.store.root, row[1])
            return p.stat().st_size == row[3] and digest_file(p) == row[2]
        except (OSError, ValueError):
            return False

    def media_done(self, key, path, digest, size):
        self.db.execute(
            "INSERT OR REPLACE INTO media VALUES (?,?,?,?,?,?)",
            (key, "done", str(path.relative_to(self.store.root)), digest, size, None),
        )
        if not self.importing:
            self.db.commit()

    def media_status(self, key, status, reason):
        self.db.execute(
            "INSERT OR REPLACE INTO media VALUES (?,?,?,?,?,?)",
            (key, status, None, None, 0, reason),
        )
        if not self.importing:
            self.db.commit()

    def issue(self, where, error, context):
        category, reason, is_error = classify(where, error)
        self.db.execute(
            "INSERT OR REPLACE INTO issues VALUES (?,?,?,?,?,?)",
            (
                issue_key(where, context),
                category,
                reason,
                int(is_error),
                1,
                json.dumps(context, ensure_ascii=False),
            ),
        )
        if not self.importing:
            self.db.commit()
        return category, reason, is_error

    def resolve(self, where, context):
        self.db.execute(
            "UPDATE issues SET active=0 WHERE key=?", (issue_key(where, context),)
        )
        if not self.importing:
            self.db.commit()

    def counts(self):
        errors, warnings = self.db.execute(
            "SELECT coalesce(sum(is_error),0),coalesce(sum(1-is_error),0) FROM issues WHERE active=1"
        ).fetchone()
        media = self.db.execute(
            "SELECT count(DISTINCT path) FROM media WHERE status='done'"
        ).fetchone()[0]
        failed = self.db.execute(
            "SELECT count(*) FROM media WHERE status='failed'"
        ).fetchone()[0]
        return {
            "errors": errors,
            "warnings": warnings,
            "media_files": media,
            "failed_media": failed,
        }

    def export_summary(self):
        rows = self.db.execute(
            "SELECT category,reason,count(*) FROM issues WHERE active=1 GROUP BY category,reason"
        ).fetchall()
        atomic_json(
            self.store.index / "issue_summary.json",
            [{"category": c, "reason": r, "count": n} for c, r, n in rows],
        )

    def statistics_done(self, key):
        row = self.db.execute(
            "SELECT state FROM statistics_windows WHERE key=?", (key,)
        ).fetchone()
        return bool(row and row[0] == "done")

    def statistics_commit(self, key, endpoint, date_from, date_to, records):
        """Atomically commit a complete normalized window and its checkpoint."""
        fetched_at = datetime.now(UTC).isoformat()
        with self.db:
            for row in records:
                self.db.execute(
                    "INSERT OR REPLACE INTO statistics_records VALUES (?,?,?,?,?,?,?,?)",
                    (
                        endpoint,
                        date_from,
                        date_to,
                        str(row["grouping_type"]),
                        str(row["record_id"]),
                        str(row["metric"]),
                        json.dumps(row.get("value"), ensure_ascii=False),
                        fetched_at,
                    ),
                )
            self.db.execute(
                "INSERT OR REPLACE INTO statistics_windows VALUES (?,?,?,?,?,?,?,NULL)",
                (key, endpoint, date_from, date_to, "done", len(records), fetched_at),
            )

    def statistics_failed(self, key, endpoint, date_from, date_to, error):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO statistics_windows VALUES (?,?,?,?,?,?,?,?)",
                (
                    key,
                    endpoint,
                    date_from,
                    date_to,
                    "failed",
                    0,
                    datetime.now(UTC).isoformat(),
                    type(error).__name__,
                ),
            )

    def statistics_counts(self):
        periods = self.db.execute(
            "SELECT count(*) FROM statistics_windows WHERE state='done'"
        ).fetchone()[0]
        records = self.db.execute(
            "SELECT count(*) FROM statistics_records"
        ).fetchone()[0]
        return periods, records


class ReplayClient:
    def __init__(self, network, journal):
        self.network, self.journal = network, journal
        self.reused = 0

    def authenticate(self):
        return self.network.authenticate()

    def close(self):
        self.network.close()

    def get(self, path, *, params=None, attempts=5):
        # Voice URLs expire; request singleton IDs afresh only when their file is absent.
        raw = (
            None
            if path.endswith("/getVoiceFiles")
            else self.journal.cached(path, params)
        )
        if raw is not None:
            self.reused += 1
            if raw.status_code >= 300:
                raise AvitoApiError(
                    f"Cached API response: HTTP {raw.status_code}",
                    status_code=raw.status_code,
                    body=raw.content.decode("utf-8", errors="replace"),
                )
            return raw
        raw = self.network.get(path, params=params, attempts=attempts)
        raw.params = params
        # Also supports deterministic offline clients without an on_request hook.
        if self.journal.cached(path, params) is None and not path.endswith(
            "/getVoiceFiles"
        ):
            self.journal.store.record_request(raw)
            self.journal.index_response(
                raw,
                self.journal.store.raw
                / "requests"
                / f"{self.journal.store._request_seq:07d}.json",
            )
        return raw

    def post(self, path, *, json_body, attempts=5):
        raw = self.journal.cached(path, json_body)
        if raw is not None:
            self.reused += 1
            if raw.status_code >= 300:
                raise AvitoApiError(
                    f"Cached API response: HTTP {raw.status_code}",
                    status_code=raw.status_code,
                    body=raw.content.decode("utf-8", errors="replace"),
                )
            return raw
        raw = self.network.post(path, json_body=json_body, attempts=attempts)
        raw.params = json_body
        if self.journal.cached(path, json_body) is None:
            self.journal.store.record_request(raw)
            self.journal.index_response(
                raw,
                self.journal.store.raw
                / "requests"
                / f"{self.journal.store._request_seq:07d}.json",
            )
        return raw

    def download(self, url, **kwargs):
        return self.network.download(url, **kwargs)

    def download_to(self, url, path):
        if hasattr(self.network, "download_to"):
            return self.network.download_to(url, path)
        raw = self.network.download(url)
        path.write_bytes(raw.content)
        return raw


def unfinished_exports(root: Path):
    result = []
    candidates = (
        [root / "manifest.json"]
        if (root / "manifest.json").exists()
        else list(root.glob("*/manifest.json"))
    )
    for path in candidates:
        try:
            manifest = json.loads(path.read_bytes())
            if manifest.get("format") in (
                "avito-raw-export-v1",
                "avito-raw-export-v2",
                "avito-raw-export-v3",
            ) and manifest.get("status") in (
                "running",
                "partial",
                "failed",
                "interrupted",
            ):
                result.append((path.parent, manifest))
        except (OSError, ValueError):
            continue
    return sorted(
        result, key=lambda row: (row[0] / "manifest.json").stat().st_mtime, reverse=True
    )
