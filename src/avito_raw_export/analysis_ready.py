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
    conversations_output = output / "conversations"
    human_output = output / "human_readable"
    legacy_chats_output = output / "chats"
    conversations_output.mkdir(parents=True, exist_ok=True)
    human_output.mkdir(parents=True, exist_ok=True)
    legacy_chats_output.mkdir(parents=True, exist_ok=True)

    chat_ids = _load_chat_ids(archive_root)
    items = _load_items(archive_root)
    index_rows: list[dict[str, Any]] = []
    conversations: list[dict[str, Any]] = []
    total_messages = 0

    for chat_id in chat_ids:
        safe_id = file_id(chat_id)
        chat_detail = _load_json_object(
            archive_root / "raw" / "chats" / "details" / f"{safe_id}.json"
        )
        messages = _load_messages(archive_root / "raw" / "messages" / safe_id)
        conversation = _conversation(chat_id, safe_id, chat_detail, messages, items)
        conversations.append(conversation)
        total_messages += len(conversation["messages"])

        json_name = f"conversation_{safe_id}.json"
        txt_name = f"conversation_{safe_id}.txt"
        atomic_json(conversations_output / json_name, conversation)
        atomic_bytes(
            human_output / txt_name,
            _render_chat_txt(conversation).encode("utf-8"),
        )
        legacy_json_name = f"chat_{safe_id}.json"
        legacy_txt_name = f"chat_{safe_id}.txt"
        atomic_json(legacy_chats_output / legacy_json_name, conversation)
        atomic_bytes(
            legacy_chats_output / legacy_txt_name,
            _render_chat_txt(conversation).encode("utf-8"),
        )
        index_rows.append(
            {
                "conversation_id": safe_id,
                "conversation_type": conversation.get("conversation_type"),
                "chat_id": chat_id,
                "item_id": conversation.get("item_id"),
                "item_title": conversation.get("item_title"),
                "first_message_at": conversation.get("first_message_at"),
                "last_message_at": conversation.get("last_message_at"),
                "message_count": conversation.get("message_count", 0),
                "incoming_count": conversation.get("incoming_count", 0),
                "outgoing_count": conversation.get("outgoing_count", 0),
                "json_path": f"conversations/{json_name}",
                "txt_path": f"human_readable/{txt_name}",
            }
        )

    atomic_json(output / "items.json", list(items.values()))
    atomic_json(output / "corpus_manifest.json", _corpus_manifest(conversations, items))
    _write_jsonl(
        output / "conversations.jsonl",
        [_compact_conversation(conversation) for conversation in conversations],
    )
    _write_csv(output / "conversations_index.csv", index_rows)
    atomic_json(output / "chats_index.json", _legacy_index(index_rows))
    _write_csv(output / "chats_index.csv", _legacy_index(index_rows), legacy=True)
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


def _conversation(
    chat_id: str,
    safe_id: str,
    chat_detail: dict[str, Any],
    messages: list[dict[str, Any]],
    items: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    incoming = sum(1 for message in messages if message.get("direction") == "incoming")
    outgoing = sum(1 for message in messages if message.get("direction") == "outgoing")
    item_id = _extract_item_id(chat_detail)
    item = items.get(str(item_id)) if item_id is not None else None
    item_context = item or _context_item(chat_detail.get("context"))
    conversation_type = _conversation_type(chat_detail, item_id)
    first = messages[0]["datetime"] if messages else None
    last = messages[-1]["datetime"] if messages else None
    result: dict[str, Any] = {
        "conversation_id": safe_id,
        "conversation_type": conversation_type,
        "chat_id": chat_id,
        "item_context": item_context,
        "context": chat_detail.get("context") if chat_detail else None,
        "item_title": _item_title(item_context) or _chat_title(chat_detail),
        "category": _item_category(item_context),
        "url": _item_url(item_context),
        "status": _item_status(item_context),
        "price": _item_price(item_context),
        "first_message_at": first,
        "last_message_at": last,
        "message_count": len(messages),
        "incoming_count": incoming,
        "outgoing_count": outgoing,
        "messages": messages,
    }
    if item_id is not None:
        result["item_id"] = item_id
    if chat_detail:
        result["chat"] = chat_detail
    return result


def _analysis_message(message: dict[str, Any]) -> dict[str, Any]:
    timestamp = _timestamp(message)
    direction = _direction(message)
    message_type = _message_type(message)
    author_id = _author_id(message)
    result: dict[str, Any] = {
        "message_id": _message_id(message),
        "timestamp": timestamp,
        "datetime": _datetime(timestamp),
        "direction": direction,
        "role": _role(direction, message_type, author_id, message),
        "author_id": author_id,
        "type": message_type,
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


def _role(
    direction: str, message_type: str, author_id: str | None, message: dict[str, Any]
) -> str:
    if _is_system_message(message, message_type, author_id):
        return "avito_system"
    if direction == "incoming":
        return "client"
    if direction == "outgoing":
        return "seller"
    return "unknown"


def _is_system_message(
    message: dict[str, Any], message_type: str, author_id: str | None
) -> bool:
    if author_id == "0":
        return True
    normalized_type = message_type.lower()
    if normalized_type in {"system", "a2u", "avito_system", "notification"}:
        return True
    author = message.get("author") or message.get("user") or message.get("sender")
    if isinstance(author, dict):
        name = str(_first_present(author, ("name", "type", "role")) or "").lower()
        if "avito" in name or "system" in name:
            return True
    return False


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


def _write_csv(path: Path, rows: list[dict[str, Any]], *, legacy: bool = False) -> None:
    fields = (
        [
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
        if legacy
        else [
            "conversation_id",
            "conversation_type",
            "chat_id",
            "item_id",
            "item_title",
            "first_message_at",
            "last_message_at",
            "message_count",
            "incoming_count",
            "outgoing_count",
            "json_path",
            "txt_path",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(
        json.dumps(row, ensure_ascii=False, default=str) + "\n" for row in rows
    )
    atomic_bytes(path, body.encode("utf-8"))


def _compact_conversation(conversation: dict[str, Any]) -> dict[str, Any]:
    return {
        "conversation_id": conversation.get("conversation_id"),
        "conversation_type": conversation.get("conversation_type"),
        "chat_id": conversation.get("chat_id"),
        "item": _compact_item(conversation.get("item_context"), conversation),
        "first_message_at": conversation.get("first_message_at"),
        "last_message_at": conversation.get("last_message_at"),
        "message_count": conversation.get("message_count", 0),
        "messages": [
            _compact_message(message)
            for message in conversation.get("messages", [])
            if isinstance(message, dict)
        ],
    }


def _compact_item(item: Any, conversation: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(item, dict) and conversation.get("item_id") is None:
        return None
    item = item if isinstance(item, dict) else {}
    result = {
        "id": _first_non_none(
            conversation.get("item_id"), _first_present(item, ("id", "item_id", "itemId"))
        ),
        "title": _first_non_none(conversation.get("item_title"), _item_title(item)),
        "category": _first_non_none(conversation.get("category"), _item_category(item)),
        "price": _first_non_none(conversation.get("price"), _item_price(item)),
        "status": _first_non_none(conversation.get("status"), _item_status(item)),
        "location": _item_location(item),
        "url": _first_non_none(conversation.get("url"), _item_url(item)),
    }
    return {key: value for key, value in result.items() if value is not None}


def _compact_message(message: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "message_id": message.get("message_id"),
        "datetime": message.get("datetime"),
        "timestamp": message.get("timestamp"),
        "role": message.get("role"),
        "type": message.get("type"),
        "text": message.get("text", ""),
    }
    if _has_media(message):
        result["has_media"] = True
    metadata = _compact_content_metadata(message.get("content"))
    if metadata:
        result["metadata"] = metadata
    return {key: value for key, value in result.items() if value is not None}


def _has_media(message: dict[str, Any]) -> bool:
    kind = str(message.get("type") or "").lower()
    if any(token in kind for token in ("image", "photo", "voice", "video", "file")):
        return True
    content = message.get("content")
    return isinstance(content, dict) and any(
        key in content for key in ("image", "images", "photo", "photos", "voice", "video", "file")
    )


def _compact_content_metadata(content: Any) -> dict[str, Any]:
    if not isinstance(content, dict):
        return {}
    metadata: dict[str, Any] = {}
    for key in ("link", "item", "location", "call"):
        value = content.get(key)
        if isinstance(value, dict):
            metadata[key] = {
                child_key: child_value
                for child_key, child_value in value.items()
                if child_key in {"title", "text", "type", "status"}
                and not _looks_like_technical_url(child_value)
            }
        elif isinstance(value, (str, int, float, bool)) and not _looks_like_technical_url(value):
            metadata[key] = value
    return {key: value for key, value in metadata.items() if value}


def _looks_like_technical_url(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(("http://", "https://"))


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


def _load_items(archive_root: Path) -> dict[str, dict[str, Any]]:
    items: dict[str, dict[str, Any]] = {}
    for path in sorted((archive_root / "raw" / "items" / "details").glob("*.json")):
        item = _load_json_object(path)
        item_id = _first_present(item, ("id", "item_id", "itemId"))
        if item_id is not None:
            items[str(item_id)] = item
    for path in sorted((archive_root / "raw" / "items" / "lists").rglob("*.json")):
        payload = _load_json_object(path)
        resources = payload.get("resources")
        if not isinstance(resources, list):
            continue
        for item in resources:
            if not isinstance(item, dict):
                continue
            item_id = _first_present(item, ("id", "item_id", "itemId"))
            if item_id is not None:
                items.setdefault(str(item_id), item)
    return dict(sorted(items.items()))


def _context_item(context: Any) -> dict[str, Any] | None:
    if not isinstance(context, dict):
        return None
    value = context.get("value")
    if isinstance(value, dict):
        return value
    return context


def _item_title(item: dict[str, Any] | None) -> str | None:
    if not isinstance(item, dict):
        return None
    value = _first_present(item, ("title", "name"))
    return str(value) if value is not None else None


def _item_category(item: dict[str, Any] | None) -> Any:
    if not isinstance(item, dict):
        return None
    return _first_present(item, ("category", "category_name", "categoryName"))


def _item_url(item: dict[str, Any] | None) -> str | None:
    if not isinstance(item, dict):
        return None
    value = _first_present(item, ("url", "uri", "link"))
    return str(value) if value is not None else None


def _item_status(item: dict[str, Any] | None) -> Any:
    if not isinstance(item, dict):
        return None
    return _first_present(item, ("status", "state"))


def _item_price(item: dict[str, Any] | None) -> Any:
    if not isinstance(item, dict):
        return None
    return _first_present(item, ("price", "price_value", "priceValue"))


def _item_location(item: dict[str, Any] | None) -> Any:
    if not isinstance(item, dict):
        return None
    return _first_present(item, ("location", "address", "city", "region"))


def _conversation_type(chat: dict[str, Any], item_id: str | int | None) -> str:
    chat_type = str(_first_present(chat, ("type", "chat_type", "chatType")) or "").lower()
    context = chat.get("context")
    context_type = ""
    if isinstance(context, dict):
        context_type = str(_first_present(context, ("type", "kind")) or "").lower()
    users = chat.get("users")
    if chat_type == "a2u" or context_type == "a2u":
        return "avito_system"
    if chat_type == "support" or context_type == "support":
        return "avito_support"
    if isinstance(users, list) and any(_is_support_user(user) for user in users):
        return "avito_support"
    if isinstance(users, list) and any(_is_system_user(user) for user in users):
        return "avito_system"
    if chat_type == "u2i" or item_id is not None:
        return "customer_item"
    return "other"


def _is_support_user(user: Any) -> bool:
    if not isinstance(user, dict):
        return False
    value = str(_first_present(user, ("type", "role", "name")) or "").lower()
    return "support" in value or "поддерж" in value


def _is_system_user(user: Any) -> bool:
    if not isinstance(user, dict):
        return False
    user_id = _first_present(user, ("id", "user_id", "userId"))
    if str(user_id) == "0":
        return True
    value = str(_first_present(user, ("type", "role", "name")) or "").lower()
    return "avito" in value or "system" in value


def _corpus_manifest(
    conversations: list[dict[str, Any]], items: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    first_values = [
        row.get("first_message_at")
        for row in conversations
        if row.get("first_message_at")
    ]
    last_values = [
        row.get("last_message_at")
        for row in conversations
        if row.get("last_message_at")
    ]
    missing_item_context = sum(1 for row in conversations if not row.get("item_context"))
    conversation_types = {
        "customer_item": 0,
        "avito_system": 0,
        "avito_support": 0,
        "other": 0,
    }
    for row in conversations:
        kind = row.get("conversation_type")
        conversation_types[str(kind) if kind in conversation_types else "other"] += 1
    missing_timestamps = sum(
        1
        for row in conversations
        for message in row.get("messages", [])
        if isinstance(message, dict) and message.get("timestamp") is None
    )
    return {
        "format": "avito-raw-export-analysis-ready-v1",
        "created_at": datetime.now(UTC).isoformat(),
        "conversation_count": len(conversations),
        "message_count": sum(
            int(row.get("message_count") or 0) for row in conversations
        ),
        "conversation_types": conversation_types,
        "item_count": len(items),
        "first_message_at": min(first_values) if first_values else None,
        "last_message_at": max(last_values) if last_values else None,
        "sources": [
            "manifest.json",
            "index/discovered_chat_ids.json",
            "raw/chats/details/*.json",
            "raw/messages/*/offset_*.json",
            "raw/items/details/*.json",
            "raw/items/lists/**/*.json",
        ],
        "limitations": {
            "raw_is_source_of_truth": True,
            "generated_without_api_requests": True,
            "missing_item_context_conversations": missing_item_context,
            "messages_without_timestamp": missing_timestamps,
            "classification_or_summary": False,
        },
    }


def _legacy_index(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "safe_chat_id": row.get("conversation_id"),
            "chat_id": row.get("chat_id"),
            "item_id": row.get("item_id"),
            "title": row.get("item_title"),
            "chat_type": None,
            "first_message_at": row.get("first_message_at"),
            "last_message_at": row.get("last_message_at"),
            "message_count": row.get("message_count"),
            "incoming_message_count": row.get("incoming_count"),
            "outgoing_message_count": row.get("outgoing_count"),
            "json_file": row.get("json_path"),
            "txt_file": row.get("txt_path"),
        }
        for row in rows
    ]


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


def _first_non_none(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _readme() -> str:
    return (
        "analysis_ready — AI-ready корпус переписок Avito.\n\n"
        "conversations.jsonl — главный машинный артефакт: одна строка JSON = "
        "одна полная переписка с контекстом объявления.\n"
        "conversations_index.csv — общий список переписок для фильтрации.\n"
        "items.json — справочник объявлений, найденных в RAW.\n"
        "conversations/*.json — один самодостаточный чат в структурированном "
        "формате.\n"
        "human_readable/*.txt — человекочитаемые переписки, их удобно открывать или "
        "загружать в ChatGPT.\n"
        "corpus_manifest.json — счётчики, период данных, источники и ограничения.\n\n"
        "RAW остаётся исходным источником истины. Эта папка построена локально, "
        "без новых запросов к Avito, и не изменяет raw/.\n\n"
        "AI-классификация, summary, RAG и оценка качества диалогов здесь не "
        "выполняются.\n"
    )
