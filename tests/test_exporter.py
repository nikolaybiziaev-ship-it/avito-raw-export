from __future__ import annotations

import json
from pathlib import Path

from avito_raw_export.client import RawResponse
from avito_raw_export.exporter import Exporter, ExportOptions


def raw(url: str, payload, content_type: str = "application/json") -> RawResponse:
    content = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return RawResponse(
        method="GET",
        url=url,
        params=None,
        status_code=200,
        headers={"content-type": content_type},
        content=content,
    )


class FakeClient:
    def __init__(self):
        self.calls = []

    def authenticate(self):
        return {"access_token": "not-written-to-export"}

    def close(self):
        pass

    def get(self, path, *, params=None, attempts=5):
        self.calls.append(("GET", path, params))
        if path == "/core/v1/accounts/self":
            return raw(path, {"id": 777, "name": "Test"})
        if path == "/core/v1/items":
            status = (
                dict(params).get("status")
                if not isinstance(params, dict)
                else params.get("status")
            )
            if dict(params).get("page", 1) > 1:
                return raw(path, {"resources": []})
            if status == "active":
                return raw(
                    path,
                    {
                        "meta": {"page": 1, "per_page": 100},
                        "resources": [{"id": 111, "status": "active", "title": "A"}],
                    },
                )
            return raw(path, {"meta": {"page": 1, "per_page": 100}, "resources": []})
        if path == "/core/v1/accounts/777/items/111/":
            return raw(
                path, {"status": "active", "url": "https://www.avito.ru/item/111"}
            )
        if path == "/ratings/v1/info":
            return raw(
                path,
                {
                    "isEnabled": True,
                    "rating": {
                        "score": 4.9,
                        "reviewsCount": 2,
                        "reviewsWithScoreCount": 2,
                    },
                },
            )
        if path == "/ratings/v1/reviews":
            offset = (
                dict(params).get("offset")
                if not isinstance(params, dict)
                else params.get("offset")
            )
            if offset == 0:
                return raw(
                    path,
                    {
                        "total": 2,
                        "reviews": [
                            {
                                "id": 9001,
                                "score": 5,
                                "stage": "done",
                                "text": "Отлично",
                                "usedInScore": True,
                                "canAnswer": True,
                                "createdAt": 1700000100,
                                "sender": {"name": "Client A"},
                                "item": {"id": 111, "title": "A"},
                            },
                            {
                                "id": 9002,
                                "score": 4,
                                "stage": "done",
                                "text": "Хорошо",
                                "usedInScore": True,
                                "canAnswer": False,
                                "createdAt": 1700000200,
                                "sender": {"name": "Client B"},
                                "item": {"id": 222, "title": "Old item"},
                            },
                        ],
                    },
                )
            return raw(path, {"total": 2, "reviews": []})
        if path == "/core/v1/accounts/777/items/222/":
            return raw(path, {"status": "old", "url": "https://www.avito.ru/item/222"})
        if path.endswith("/chats"):
            if int(dict(params).get("offset", 0)):
                return raw(path, {"chats": []})
            return raw(
                path, {"chats": [{"id": "chat-1", "context": {"value": {"id": 111}}}]}
            )
        if path.endswith("/chats/chat-1"):
            return raw(
                path,
                {
                    "id": "chat-1",
                    "users": [{"id": 1, "name": "Client"}],
                    "context": {"value": {"id": 111}},
                },
            )
        if path.endswith("/messages/"):
            if int(dict(params).get("offset", 0)):
                return raw(path, [])
            return raw(
                path,
                [
                    {
                        "id": "m1",
                        "created": 1700000000,
                        "direction": "in",
                        "type": "voice",
                        "content": {"voice": {"voice_id": "voice-1"}},
                    }
                ],
            )
        if path.endswith("/getVoiceFiles"):
            return raw(
                path, {"voices_urls": {"voice-1": "https://cdn.avito.ru/voice-1.opus"}}
            )
        raise AssertionError(f"unexpected GET {path} {params}")

    def download(self, url, *, attempts=3):
        self.calls.append(("DOWNLOAD", url, None))
        return raw(url, b"VOICE", "audio/ogg")


def test_full_mock_export(tmp_path: Path):
    exporter = Exporter(
        "client",
        "secret",
        ExportOptions(export_root=tmp_path, download_avito_media=False),
    )
    exporter.client.close()
    fake = FakeClient()
    exporter.client = fake
    result = exporter.run()

    manifest = json.loads((result / "manifest.json").read_text())
    assert manifest["status"] == "completed"
    assert manifest["counts"]["errors"] == 0
    assert manifest["account_id"] == 777
    assert manifest["counts"]["items"] == 2
    assert manifest["counts"]["chats"] == 1
    assert manifest["counts"]["unique_reviews"] == 2
    assert manifest["counts"]["reviews_seen"] == 2
    assert manifest["counts"]["messages_seen"] == 1
    assert manifest["oldest_message_timestamp"] == 1700000000
    assert (result / "raw" / "messages" / "chat-1" / "offset_0000.json").exists()
    assert (result / "raw" / "ratings" / "info.json").exists()
    assert (result / "raw" / "ratings" / "reviews" / "offset_0000000.json").exists()
    assert json.loads((result / "index" / "review_ids.json").read_text()) == [
        9001,
        9002,
    ]
    assert list((result / "media" / "voice").iterdir())

    # Runtime data must never contain write Messenger endpoints.
    paths = [call[1] for call in fake.calls if call[0] == "GET"]
    assert not any(path.endswith("/read") for path in paths)
    assert not any("blacklist" in path for path in paths)
