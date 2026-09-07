from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .identifiers import file_id
from .store import atomic_bytes, atomic_json


@dataclass(frozen=True)
class AnalysisReadyResult:
    root: Path
    chats: int
    messages: int


def build_analysis_ready(archive_root: Path) -> AnalysisReadyResult:
    archive_root = archive_root.resolve()
    _validate_archive(archive_root)
    output = archive_root / "analysis_ready"
    chats_output = output / "chats"
    chats_output.mkdir(parents=True, exist_ok=True)

    chat_ids = _load_chat_ids(archive_root)
    index_rows: list[dict[str, Any]] = []
    total_messages = 0

    for chat_id in chat_ids:
        safe_id = file_id(chat_id)
        chat_detail = _load_json_object(
            archive_root / "raw" / "chats" / "details" / f"{safe_id}.json"
        )
        messages = _load_messages(archive_root / "raw" / "messages" / safe_id)
        analysis_chat = _analysis_chat(chat_id, chat_detail, messages)
        total_messages += len(analysis_chat["messages"])

        json_name = f"chat_{safe_id}.json"
        txt_name = f"chat_{safe_id}.txt"
        atomic_json(chats_output / json_name, analysis_chat)
        atomic_bytes(
            chats_output / txt_name,
            _render_chat_txt(analysis_chat).encode("utf-8"),
        )
        index_rows.append(
            {
                "safe_chat_id": safe_id,
                "chat_id": chat_id,
                "item_id": analysis_chat.get("item_id"),
                "title": _chat_title(chat_detail),
                "chat_type": _first_present(chat_detail, ("type", "chat_type", "chatType")),
                "first_message_at": analysis_chat.get("first_message_at"),
                "last_message_at": analysis_chat.get("last_message_at"),
                "message_count": analysis_chat.get("message_count", 0),
                "incoming_message_count": analysis_chat.get(
                    "incoming_message_count", 0
                ),
                "outgoing_message_count": analysis_chat.get(
                    "outgoing_message_count", 0
                ),
                "json_file": f"chats/{json_name}",
                "txt_file": f"chats/{txt_name}",
            }
        )

    atomic_json(output / "chats_index.json", index_rows)
    _write_csv(output / "chats_index.csv", index_rows)
    atomic_bytes((output / "README.txt"), _readme().encode("utf-8"))
    return AnalysisReadyResult(output, len(index_rows), total_messages)


def _validate_archive(archive_root: Path) -> None:
    manifest = archive_root / "manifest.json"
    raw = archive_root / "raw"
    if not manifest.exists() or not raw.is_dir():
        raise ValueError("Выберите папку архива Avito Raw Export")


def _load_chat_ids(archive_root: Path) -> list[str]:
    discovered = archive_root / "index" / "discovered_chat_ids.json"
    if discovered.exists():
        payload = json.loads(discovered.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            return sorted({str(value) for value in payload if value is not None})
    message_root = archive_root / "raw" / "messages"
    if not message_root.exists():
        return []
    return sorted(path.name for path in message_root.iterdir() if path.is_dir())


def _load_messages(message_root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    if not message_root.exists():
        return rows
    for path in sorted(message_root.glob("offset_*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        messages = payload if isinstance(payload, list) else payload.get("messages")
        if not isinstance(messages, list):
            continue
        for message in messages:
            if not isinstance(message, dict):
                continue
            message_id = _message_id(message)
            if message_id and message_id in seen_ids:
                continue
            if message_id:
                seen_ids.add(message_id)
            rows.append(_analysis_message(message))
    return sorted(rows, key=_message_sort_key)


def _analysis_chat(
    chat_id: str, chat_detail: dict[str, Any], messages: list[dict[str, Any]]
) -> dict[str, Any]:
    incoming = sum(1 for message in messages if message.get("direction") == "incoming")
    outgoing = sum(1 for message in messages if message.get("direction") == "outgoing")
    item_id = _extract_item_id(chat_detail)
    first = messages[0]["datetime"] if messages else None
    last = messages[-1]["datetime"] if messages else None
    result: dict[str, Any] = {
        "chat_id": chat_id,
        "context": chat_detail.get("context") if chat_detail else None,
        "first_message_at": first,
        "last_message_at": last,
        "message_count": len(messages),
        "incoming_message_count": incoming,
        "outgoing_message_count": outgoing,
        "messages": messages,
    }
    if item_id is not None:
        result["item_id"] = item_id
    if chat_detail:
        result["chat"] = chat_detail
    return result


def _analysis_message(message: dict[str, Any]) -> dict[str, Any]:
    timestamp = _timestamp(message)
    result: dict[str, Any] = {
        "message_id": _message_id(message),
        "timestamp": timestamp,
        "datetime": _datetime(timestamp),
        "direction": _direction(message),
        "author_id": _author_id(message),
        "type": _message_type(message),
        "text": _message_text(message),
        "content": message.get("content"),
    }
    return {key: value for key, value in result.items() if value is not None}


def _message_id(message: dict[str, Any]) -> str | None:
    value = _first_present(message, ("id", "message_id", "messageId"))
    return str(value) if value is not None else None


def _timestamp(message: dict[str, Any]) -> int | str | None:
    return _first_present(message, ("created", "created_at", "createdAt", "timestamp"))


def _datetime(timestamp: int | str | None) -> str | None:
    if timestamp is None:
        return None
    try:
        if isinstance(timestamp, str) and not timestamp.isdigit():
            return timestamp
        return datetime.fromtimestamp(int(timestamp), UTC).isoformat()
    except (TypeError, ValueError, OSError):
        return str(timestamp)


def _direction(message: dict[str, Any]) -> str:
    value = str(_first_present(message, ("direction", "flow")) or "").lower()
    if value in {"in", "incoming", "inbound", "received"}:
        return "incoming"
    if value in {"out", "outgoing", "outbound", "sent"}:
        return "outgoing"
    current_user = _first_present(message, ("isReadByUser", "is_read_by_user"))
    if isinstance(current_user, bool):
        return "incoming" if not current_user else "outgoing"
    return "unknown"


def _author_id(message: dict[str, Any]) -> str | None:
    author = message.get("author") or message.get("user") or message.get("sender")
    if isinstance(author, dict):
        value = _first_present(author, ("id", "user_id", "userId"))
    else:
        value = _first_present(message, ("author_id", "authorId", "user_id", "userId"))
    return str(value) if value is not None else None


def _message_type(message: dict[str, Any]) -> str:
    explicit = _first_present(message, ("type", "message_type", "messageType"))
    if explicit:
        return str(explicit)
    content = message.get("content")
    if isinstance(content, dict):
        for key in ("text", "image", "voice", "link", "item", "location", "call"):
            if key in content:
                return key
    return "system"


def _message_text(message: dict[str, Any]) -> str:
    direct = _first_present(message, ("text", "body", "message"))
    if direct is not None:
        return str(direct)
    content = message.get("content")
    if isinstance(content, dict):
        for key in ("text", "caption", "description"):
            value = content.get(key)
            if isinstance(value, str):
                return value
        text = content.get("text")
        if isinstance(text, dict):
            value = _first_present(text, ("text", "value"))
            if value is not None:
                return str(value)
    return ""


def _message_sort_key(message: dict[str, Any]) -> tuple[int, str]:
    timestamp = message.get("timestamp")
    try:
        return (int(timestamp), str(message.get("message_id") or ""))
    except (TypeError, ValueError):
        return (2**63 - 1, str(message.get("message_id") or ""))


def _render_chat_txt(chat: dict[str, Any]) -> str:
    lines = [
        f"Чат: {chat.get('chat_id', '')}",
        f"Объявление: {_context_title(chat.get('context'))}",
        f"Период: {chat.get('first_message_at') or ''} — {chat.get('last_message_at') or ''}",
        f"Сообщений: {chat.get('message_count', 0)}",
        "",
    ]
    for message in chat.get("messages", []):
        if not isinstance(message, dict):
            continue
        lines.append(
            f"[{message.get('datetime') or message.get('timestamp') or ''}] "
            f"{_speaker(message.get('direction'))}:"
        )
        body = message.get("text") or _placeholder(message.get("type"))
        if message.get("text") and _placeholder(message.get("type")):
            body = f"{_placeholder(message.get('type'))} {message['text']}"
        lines.extend([str(body), ""])
    return "\n".join(lines).rstrip() + "\n"


def _speaker(direction: Any) -> str:
    if direction == "incoming":
        return "Клиент"
    if direction == "outgoing":
        return "Мы"
    return "Неизвестный участник"


def _placeholder(kind: Any) -> str:
    normalized = str(kind or "").lower()
    if "voice" in normalized:
        return "[голосовое]"
    if any(token in normalized for token in ("image", "photo", "picture")):
        return "[изображение]"
    if "link" in normalized:
        return "[ссылка]"
    if "item" in normalized:
        return "[товар/объявление]"
    if "system" in normalized:
        return "[системное сообщение]"
    return ""


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "safe_chat_id",
        "chat_id",
        "item_id",
        "title",
        "chat_type",
        "first_message_at",
        "last_message_at",
        "message_count",
        "incoming_message_count",
        "outgoing_message_count",
        "json_file",
        "txt_file",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def _load_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def _extract_item_id(chat: dict[str, Any]) -> str | int | None:
    context = chat.get("context")
    if isinstance(context, dict):
        value = context.get("value")
        if isinstance(value, dict):
            item_id = _first_present(value, ("id", "item_id", "itemId"))
            if item_id is not None:
                return item_id
    return _first_present(chat, ("item_id", "itemId"))


def _chat_title(chat: dict[str, Any]) -> str | None:
    title = _first_present(chat, ("title", "item_title", "itemTitle"))
    if title is not None:
        return str(title)
    context = chat.get("context")
    return _context_title(context)


def _context_title(context: Any) -> str:
    if isinstance(context, dict):
        value = context.get("value")
        if isinstance(value, dict):
            title = _first_present(value, ("title", "name"))
            if title is not None:
                return str(title)
        title = _first_present(context, ("title", "name"))
        if title is not None:
            return str(title)
    return ""


def _first_present(mapping: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _readme() -> str:
    return (
        "analysis_ready — производный слой для чтения и анализа.\n\n"
        "chats_index.csv — общий список чатов для таблиц.\n"
        "chats_index.json — тот же индекс в JSON.\n"
        "chats/*.txt — человекочитаемые переписки, их удобно открывать или "
        "загружать в ChatGPT.\n"
        "chats/*.json — один чат в структурированном формате для программного "
        "анализа.\n\n"
        "RAW остаётся исходным источником истины. Эта папка построена локально, "
        "без новых запросов к Avito, и не изменяет raw/.\n"
    )
