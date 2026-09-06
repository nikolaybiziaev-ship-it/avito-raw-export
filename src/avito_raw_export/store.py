from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .client import RawResponse

_SAFE = re.compile(r"[^a-zA-Z0-9_.-]+")


def _safe(value: str) -> str:
    value = _SAFE.sub("_", value.strip())
    return value[:120].strip(".") or "unknown"


def replace_file(source, destination):
    # Windows scanners/readers can hold a sharing lock briefly after close.
    for attempt in range(8):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt == 7:
                raise
            time.sleep(min(0.02 * 2**attempt, 0.3))


def atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".checkpoint-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        replace_file(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def atomic_json(path: Path, payload: Any) -> None:
    atomic_bytes(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, default=str).encode("utf-8"),
    )


class ExportStore:
    def __init__(self, root: Path, account_id: int | str | None = None) -> None:
        stamp = datetime.now().astimezone().strftime("%Y-%m-%d_%H%M%S")
        self.root = root / f"{stamp}_export_{uuid.uuid4().hex[:12]}"
        self.raw = self.root / "raw"
        self.media = self.root / "media"
        self.logs = self.root / "logs"
        self.index = self.root / "index"
        for folder in (self.raw, self.media, self.logs, self.index):
            folder.mkdir(parents=True, exist_ok=True)
        self._request_seq = 0
        self.manifest: dict[str, Any] = {
            "format": "avito-raw-export-v1",
            "created_at": datetime.now(UTC).isoformat(),
            "account_id": account_id,
            "status": "running",
            "counts": {
                "items": 0,
                "chats": 0,
                "message_pages": 0,
                "messages_seen": 0,
                "review_pages": 0,
                "reviews_seen": 0,
                "unique_reviews": 0,
                "statistics_records": 0,
                "statistics_periods": 0,
                "media_files": 0,
                "errors": 0,
            },
            "oldest_message_timestamp": None,
            "newest_message_timestamp": None,
            "notes": [
                "Raw API responses are preserved as received.",
                "No write/mutation endpoints are used by the exporter.",
            ],
        }
        self.write_manifest()

    @classmethod
    def open_existing(cls, root: Path) -> "ExportStore":
        self = cls.__new__(cls)
        self.root = root.resolve()
        self.manifest = json.loads((self.root / "manifest.json").read_bytes())
        if self.manifest.get("format") not in (
            "avito-raw-export-v1",
            "avito-raw-export-v2",
            "avito-raw-export-v3",
        ):
            raise ValueError("Unsupported archive format")
        self.raw, self.media, self.logs, self.index = [
            self.root / name for name in ("raw", "media", "logs", "index")
        ]
        self._request_seq = max(
            (
                int(p.stem)
                for p in (self.raw / "requests").glob("*.json")
                if p.stem.isdigit()
            ),
            default=0,
        )
        return self

    def rename_for_account(self, account_id: int | str) -> None:
        self.manifest["account_id"] = account_id
        desired = self.root.parent / self.root.name.replace(
            "account_pending", f"account_{_safe(str(account_id))}"
        )
        if desired != self.root and not desired.exists():
            self.root.rename(desired)
            self.root = desired
            self.raw = self.root / "raw"
            self.media = self.root / "media"
            self.logs = self.root / "logs"
            self.index = self.root / "index"
        self.write_manifest()

    def record_request(self, raw: RawResponse) -> None:
        if raw.method not in ("GET", "POST"):
            return
        self._request_seq += 1
        # Preserve failed/retried responses too, before parsing or raising.
        self.save_raw(f"requests/{self._request_seq:07d}.json", raw)
        entry = {
            "seq": self._request_seq,
            "method": raw.method,
            "url": raw.url,
            "params": raw.params,
            "status_code": raw.status_code,
            "headers": self._redacted_headers(raw.headers),
            "bytes": len(raw.content),
            "sha256": hashlib.sha256(raw.content).hexdigest(),
            "recorded_at": datetime.now(UTC).isoformat(),
        }
        with (self.logs / "requests.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")

    @staticmethod
    def _redacted_headers(headers: dict[str, str]) -> dict[str, str]:
        deny = {"authorization", "cookie", "set-cookie"}
        return {
            k: ("<redacted>" if k.lower() in deny else v) for k, v in headers.items()
        }

    @staticmethod
    def _contained(root: Path, relative: str) -> Path:
        path = (root / relative).resolve()
        if not path.is_relative_to(root.resolve()) or path == root.resolve():
            raise ValueError("Export path escapes its directory")
        return path

    def save_raw(self, relative: str, raw: RawResponse) -> Path:
        path = self._contained(self.raw, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_bytes(path, raw.content)
        meta_path = path.with_suffix(path.suffix + ".meta.json")
        atomic_json(
            meta_path,
            {
                "method": raw.method,
                "url": raw.url,
                "params": raw.params,
                "status_code": raw.status_code,
                "headers": self._redacted_headers(raw.headers),
                "bytes": len(raw.content),
                "sha256": hashlib.sha256(raw.content).hexdigest(),
            },
        )
        return path

    def save_json(self, relative: str, payload: Any) -> Path:
        path = self._contained(self.index, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(path, payload)
        return path

    def save_media(
        self,
        category: str,
        name: str,
        content: bytes,
        *,
        raw: RawResponse | None = None,
    ) -> Path:
        folder = self.media / _safe(category)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / _safe(name)
        if path.exists():
            stem, suffix = path.stem, path.suffix
            digest = hashlib.sha256(content).hexdigest()[:10]
            path = folder / f"{stem}_{digest}{suffix}"
        path.write_bytes(content)
        if raw is not None:
            path.with_suffix(path.suffix + ".meta.json").write_text(
                json.dumps(
                    {
                        "url": raw.url,
                        "status_code": raw.status_code,
                        "headers": self._redacted_headers(raw.headers),
                        "bytes": len(content),
                        "sha256": hashlib.sha256(content).hexdigest(),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        self.manifest["counts"]["media_files"] += 1
        self.write_manifest()
        return path

    def error(self, where: str, error: Exception, context: Any = None) -> None:
        self.manifest["counts"]["errors"] += 1
        with (self.logs / "errors.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "at": datetime.now(UTC).isoformat(),
                        "where": where,
                        "error": str(error),
                        "context": context,
                    },
                    ensure_ascii=False,
                    default=str,
                )
                + "\n"
            )
        self.write_manifest()

    def write_manifest(self) -> None:
        atomic_json(self.root / "manifest.json", self.manifest)
