from __future__ import annotations

import csv
import json
from pathlib import Path

from avito_raw_export.analysis_ready import build_analysis_ready
from avito_raw_export.identifiers import file_id


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def make_archive(root: Path, chat_id: str = "chat/unsafe:1") -> Path:
    archive = root / "2026-09-06_120000_export_synthetic"
    write_json(archive / "manifest.json", {"format": "avito-raw-export-v3"})
    write_json(archive / "index" / "discovered_chat_ids.json", [chat_id])
    safe_id = file_id(chat_id)
    write_json(
        archive / "raw" / "chats" / "details" / f"{safe_id}.json",
        {
            "id": chat_id,
            "type": "u2i",
            "context": {"value": {"id": 111, "title": "Synthetic item"}},
        },
    )
    write_json(
        archive / "raw" / "items" / "details" / "111.json",
        {
            "id": 111,
            "title": "Synthetic item detail",
            "category": "Synthetic category",
            "url": "https://www.avito.ru/synthetic",
            "status": "active",
            "price": 12345,
        },
    )
    return archive


def test_analysis_ready_merges_pages_deduplicates_and_sorts(tmp_path: Path):
    chat_id = "chat/unsafe:1"
    safe_id = file_id(chat_id)
    archive = make_archive(tmp_path, chat_id)
    messages = archive / "raw" / "messages" / safe_id
    write_json(
        messages / "offset_0000.json",
        {
            "messages": [
                {
                    "id": "m2",
                    "created": 20,
                    "direction": "out",
                    "type": "text",
                    "content": {"text": "Second"},
                },
                {
                    "id": "m1",
                    "created": 10,
                    "direction": "in",
                    "type": "text",
                    "content": {"text": "First"},
                },
            ]
        },
    )
    write_json(
        messages / "offset_0002.json",
        [
            {
                "id": "m2",
                "created": 20,
                "direction": "out",
                "type": "text",
                "content": {"text": "Second duplicate"},
            },
            {
                "id": "m3",
                "created": 30,
                "direction": "incoming",
                "type": "image",
                "content": {"image": {"url": "https://img.example/image.jpg"}},
            },
            {
                "id": "m4",
                "created": 40,
                "direction": "outgoing",
                "type": "voice",
                "content": {"voice": {"voice_id": "v1"}, "caption": "listen"},
            },
            {
                "id": "m5",
                "created": 50,
                "type": "system",
                "content": {},
            },
            {
                "id": "m6",
                "created": 60,
                "direction": "in",
                "type": "text",
                "content": {"text": ""},
            },
        ],
    )

    result = build_analysis_ready(archive)

    conversation = json.loads(
        (result.root / "conversations" / f"conversation_{safe_id}.json").read_text(
            encoding="utf-8"
        )
    )
    assert [message["message_id"] for message in conversation["messages"]] == [
        "m1",
        "m2",
        "m3",
        "m4",
        "m5",
        "m6",
    ]
    assert conversation["message_count"] == 6
    assert conversation["incoming_count"] == 3
    assert conversation["outgoing_count"] == 2
    assert conversation["messages"][0]["direction"] == "incoming"
    assert conversation["messages"][0]["role"] == "client"
    assert conversation["messages"][1]["direction"] == "outgoing"
    assert conversation["messages"][1]["role"] == "seller"
    assert conversation["messages"][4]["role"] == "unknown"
    assert conversation["messages"][0]["text"] == "First"
    assert conversation["messages"][-1]["text"] == ""
    assert conversation["item_id"] == 111
    assert conversation["item_title"] == "Synthetic item detail"
    assert conversation["category"] == "Synthetic category"
    assert conversation["url"] == "https://www.avito.ru/synthetic"
    assert conversation["status"] == "active"
    assert conversation["price"] == 12345

    text = (result.root / "human_readable" / f"conversation_{safe_id}.txt").read_text(
        encoding="utf-8"
    )
    assert "Клиент:" in text
    assert "Мы:" in text
    assert "Неизвестный участник:" in text
    assert "[изображение]" in text
    assert "[голосовое] listen" in text
    assert "[системное сообщение]" in text


def test_analysis_ready_writes_index_and_safe_files(tmp_path: Path):
    chat_id = "chat/unsafe:1"
    safe_id = file_id(chat_id)
    archive = make_archive(tmp_path, chat_id)
    write_json(
        archive / "raw" / "messages" / safe_id / "offset_0000.json",
        [{"id": "m1", "created": 1, "direction": "in", "content": {"text": "Hi"}}],
    )

    result = build_analysis_ready(archive)

    conversations_index = json.loads(
        (result.root / "conversations.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    assert conversations_index["conversation_id"] == safe_id
    assert conversations_index["item_context"]["id"] == 111
    assert conversations_index["messages"][0]["role"] == "client"
    index = json.loads((result.root / "chats_index.json").read_text(encoding="utf-8"))
    assert index == [
        {
            "safe_chat_id": safe_id,
            "chat_id": chat_id,
            "item_id": 111,
            "title": "Synthetic item detail",
            "chat_type": None,
            "first_message_at": "1970-01-01T00:00:01+00:00",
            "last_message_at": "1970-01-01T00:00:01+00:00",
            "message_count": 1,
            "incoming_message_count": 1,
            "outgoing_message_count": 0,
            "json_file": f"conversations/conversation_{safe_id}.json",
            "txt_file": f"human_readable/conversation_{safe_id}.txt",
        }
    ]
    with (result.root / "conversations_index.csv").open(
        encoding="utf-8-sig"
    ) as stream:
        rows = list(csv.DictReader(stream))
    assert rows[0]["conversation_id"] == safe_id
    assert rows[0]["json_path"] == f"conversations/conversation_{safe_id}.json"
    assert rows[0]["txt_path"] == f"human_readable/conversation_{safe_id}.txt"
    assert "/" not in safe_id and ":" not in safe_id
    assert (result.root / "README.txt").exists()
    assert json.loads((result.root / "items.json").read_text(encoding="utf-8")) == [
        {
            "id": 111,
            "title": "Synthetic item detail",
            "category": "Synthetic category",
            "url": "https://www.avito.ru/synthetic",
            "status": "active",
            "price": 12345,
        }
    ]
    manifest = json.loads(
        (result.root / "corpus_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["conversation_count"] == 1
    assert manifest["message_count"] == 1
    assert manifest["item_count"] == 1
    assert manifest["limitations"]["generated_without_api_requests"] is True


def test_analysis_ready_uses_existing_archive_without_api_client(tmp_path: Path):
    archive = make_archive(tmp_path, "chat-1")
    write_json(
        archive / "raw" / "messages" / "chat-1" / "offset_0000.json",
        [{"id": "m1", "created": 1, "direction": "out", "content": {"text": "Hi"}}],
    )

    result = build_analysis_ready(archive)

    assert result.chats == 1
    assert result.messages == 1
    assert (archive / "raw" / "messages" / "chat-1" / "offset_0000.json").exists()
