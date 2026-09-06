import json

from test_exporter import FakeClient, raw

from avito_raw_export.exporter import Exporter, ExportOptions


def make_exporter(tmp_path, fake):
    exporter = Exporter(
        "test-client",
        "test-secret",
        ExportOptions(tmp_path, download_voice=False, download_avito_media=False),
    )
    exporter.client.close()
    exporter.client = fake
    return exporter


def test_short_message_pages_continue_and_envelope_supported(tmp_path):
    class Client(FakeClient):
        def get(self, path, *, params=None, **kwargs):
            if path.endswith("/messages/"):
                offset = params["offset"]
                return raw(
                    path,
                    {"messages": [{"id": f"m-{offset}", "created": offset}]}
                    if offset < 2
                    else {"messages": []},
                )
            return super().get(path, params=params)

    exporter = make_exporter(tmp_path, Client())
    result = exporter.run()
    manifest = json.loads((result / "manifest.json").read_text())
    assert manifest["counts"]["messages_seen"] == 2
    assert manifest["status"] == "completed"


def test_malformed_response_is_partial_and_raw_survives(tmp_path):
    class Client(FakeClient):
        def get(self, path, *, params=None, **kwargs):
            if path.endswith("/messages/"):
                return raw(path, b"not json")
            return super().get(path, params=params)

    result = make_exporter(tmp_path, Client()).run()
    assert json.loads((result / "manifest.json").read_text())["status"] == "partial"
    assert (result / "raw/messages/chat-1/offset_0000.json").read_bytes() == b"not json"


def test_repeated_reviews_stop_with_error(tmp_path):
    class Client(FakeClient):
        def get(self, path, *, params=None, **kwargs):
            if path == "/ratings/v1/reviews":
                return raw(path, {"reviews": [{"id": 1}], "total": 500})
            return super().get(path, params=params)

    exporter = make_exporter(tmp_path, Client())
    result = exporter.run()
    assert exporter.stats.reviews_seen == 1
    assert json.loads((result / "manifest.json").read_text())["status"] == "partial"


def test_message_ceiling_is_reported(tmp_path):
    class Client(FakeClient):
        def get(self, path, *, params=None, **kwargs):
            if path.endswith("/messages/"):
                return raw(
                    path, [{"id": str(params["offset"] + i)} for i in range(100)]
                )
            return super().get(path, params=params)

    exporter = make_exporter(tmp_path, Client())
    result = exporter.run()
    assert exporter.stats.message_pages == 11
    assert json.loads((result / "manifest.json").read_text())["status"] == "partial"


def test_item_discovered_only_in_chat_detail_is_scanned(tmp_path):
    class Client(FakeClient):
        def get(self, path, *, params=None, **kwargs):
            if path.endswith("/chats/chat-1"):
                return raw(path, {"id": "chat-1", "context": {"value": {"id": 333}}})
            if path.endswith("/items/333/"):
                self.calls.append(("GET", path, params))
                return raw(path, {"id": 333})
            return super().get(path, params=params)

    fake = Client()
    result = make_exporter(tmp_path, fake).run()
    assert 333 in json.loads((result / "index/discovered_item_ids.json").read_text())
    assert any(
        dict(params or {}).get("item_ids") == "333" for _, _, params in fake.calls
    )
    assert (result / "raw/items/details/333.json").exists()


def test_account_failure_marks_manifest_failed(tmp_path):
    class Client(FakeClient):
        def authenticate(self):
            raise RuntimeError("authentication unavailable")

    import pytest

    exporter = make_exporter(tmp_path, Client())
    with pytest.raises(RuntimeError):
        exporter.run()
    assert (
        json.loads((exporter.store.root / "manifest.json").read_text())["status"]
        == "failed"
    )
