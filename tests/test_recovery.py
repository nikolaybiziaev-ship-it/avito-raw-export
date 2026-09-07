import json
from pathlib import Path

import httpx
import pytest
import respx
from filelock import FileLock, Timeout

from avito_raw_export.client import AvitoClient, AvitoApiError
from avito_raw_export.exporter import Exporter, ExportOptions
from avito_raw_export.identifiers import file_id, valid_chat_id
from avito_raw_export.store import atomic_json
from test_exporter import FakeClient, raw


def make(tmp_path, client, resume=None, **options):
    options.setdefault("statistics", False)
    exporter = Exporter(
        "synthetic-client",
        "synthetic-secret",
        ExportOptions(tmp_path, resume_from=resume, **options),
    )
    exporter.client.close()
    exporter.client = client
    return exporter


@pytest.mark.parametrize(
    "identifier", ["u2i-" + "a" * 20 + "~b", "u2u-" + "~" * 22, "a2u-" + "b" * 19]
)
def test_anonymized_live_id_shapes(identifier):
    assert valid_chat_id(identifier)
    name = file_id(identifier)
    assert "/" not in name and "\\" not in name and "~" not in name
    assert file_id(identifier) == name
    assert file_id(identifier + "x") != name


@pytest.mark.parametrize(
    "identifier",
    ["../x", "..", "a/b", "a\\b", "%2e%2e", "a?x=1", "a#x", "a\0b", "a:b", "а"],
)
def test_path_attacks_rejected(identifier):
    assert not valid_chat_id(identifier)
    assert Path(file_id(identifier)).name == file_id(identifier)


def test_resume_interrupted_messages_no_repeated_network(tmp_path):
    class Interrupted(FakeClient):
        def get(self, path, *, params=None, **kwargs):
            if path.endswith("/messages/") and params["offset"] > 0:
                raise KeyboardInterrupt("synthetic interruption")
            return super().get(path, params=params)

    first = make(tmp_path, Interrupted())
    with pytest.raises(KeyboardInterrupt):
        first.run()
    folder = first.store.root
    manifest = json.loads((folder / "manifest.json").read_bytes())
    assert manifest["status"] == "interrupted"
    assert manifest["stages"]["messages"]["status"] == "interrupted"
    cached_page = folder / "raw/messages/chat-1/offset_0000.json"
    original = cached_page.read_bytes()
    second_client = FakeClient()
    second = make(tmp_path, second_client, folder)
    second.run()
    requested = [
        (p, params) for method, p, params in second_client.calls if method == "GET"
    ]
    assert not any(p == "/core/v1/items" for p, _ in requested)
    assert not any(
        p.endswith("/messages/") and params["offset"] == 0 for p, params in requested
    )
    assert cached_page.read_bytes() == original
    assert second.stats.messages_seen == 1
    assert json.loads((folder / "manifest.json").read_bytes())["status"] == "completed"


def test_resume_interrupted_media_skips_verified_file(tmp_path):
    urls = ["https://cdn.avito.ru/a.jpg", "https://cdn.avito.ru/b.jpg"]

    class MediaClient(FakeClient):
        def get(self, path, *, params=None, **kwargs):
            if path == "/core/v1/accounts/self":
                return raw(path, {"id": 777, "images": urls})
            return super().get(path, params=params)

        def download(self, url, **kwargs):
            if url == urls[1]:
                raise KeyboardInterrupt("interruption during second file")
            return super().download(url)

    first = make(
        tmp_path,
        MediaClient(),
        download_voice=False,
        download_avito_media=True,
    )
    with pytest.raises(KeyboardInterrupt):
        first.run()
    second_client = FakeClient()
    second = make(tmp_path, second_client, first.store.root)
    second.run()
    downloads = [url for method, url, _ in second_client.calls if method == "DOWNLOAD"]
    assert urls[0] not in downloads and urls[1] in downloads
    assert second.stats.media_files == 2
    assert not list(second.store.media.rglob("*.part"))


def test_foreign_item_422_is_one_warning_and_persisted(tmp_path):
    class Client(FakeClient):
        def get(self, path, *, params=None, **kwargs):
            if path.endswith("/items/222/"):
                self.calls.append(("GET", path, params))
                response = raw(
                    path,
                    {
                        "error": {
                            "code": 422,
                            "fields": {
                                "item_id": "Объявление не принадлежит пользователю"
                            },
                        }
                    },
                )
                response.status_code = 422
                self.on_request(response)
                raise AvitoApiError(
                    "validation", status_code=422, body=response.content.decode()
                )
            return super().get(path, params=params)

    client = Client()
    first = make(tmp_path, client, ratings_and_reviews=True)
    first.run()
    assert first.stats.errors == 0 and first.stats.warnings == 1
    assert sum(p.endswith("/items/222/") for method, p, _ in client.calls) == 1
    retry = Client()
    second = make(tmp_path, retry, first.store.root)
    second.run()
    assert not any(p.endswith("/items/222/") for method, p, _ in retry.calls)
    assert second.stats.warnings == 1


def test_partial_voice_uses_singletons_and_records_missing(tmp_path):
    class Client(FakeClient):
        def get(self, path, *, params=None, **kwargs):
            if path.endswith("/messages/") and not params["offset"]:
                return raw(
                    path,
                    [
                        {"id": str(i), "content": {"voice": {"voice_id": f"voice-{i}"}}}
                        for i in (1, 2)
                    ],
                )
            if path.endswith("/getVoiceFiles"):
                self.calls.append(("GET", path, params))
                assert isinstance(params, dict)
                return raw(
                    path,
                    {
                        "voices_urls": {"voice-1": "https://cdn.avito.ru/voice-1.opus"}
                        if params["voice_ids"] == "voice-1"
                        else {}
                    },
                )
            return super().get(path, params=params)

    first = make(tmp_path, Client(), download_voice=True)
    first.run()
    assert first.stats.errors == 0 and first.stats.warnings == 1
    second_client = Client()
    second = make(tmp_path, second_client, first.store.root)
    second.run()
    assert not any(p.endswith("/getVoiceFiles") for method, p, _ in second_client.calls)
    detail = json.loads((first.store.index / "voice/voice-2.json").read_bytes())
    assert detail["missing"] == ["voice-2"] and detail["returned"] == []


def test_wrong_account_cannot_modify_existing_archive(tmp_path):
    original = make(tmp_path, FakeClient())
    original.run()
    before = (original.store.root / "manifest.json").read_bytes()

    class Other(FakeClient):
        def get(self, *args, **kwargs):
            return raw("/core/v1/accounts/self", {"id": 888})

    with pytest.raises(ValueError, match="другой аккаунт"):
        make(tmp_path, Other(), original.store.root).run()
    assert (original.store.root / "manifest.json").read_bytes() == before


def test_atomic_checkpoint_preserves_previous_on_replace_failure(tmp_path, monkeypatch):
    import avito_raw_export.store as module

    path = tmp_path / "manifest.json"
    atomic_json(path, {"status": "running"})

    def fail(*args):
        raise OSError("interrupted atomic replacement")

    monkeypatch.setattr(module.os, "replace", fail)
    with pytest.raises(OSError):
        atomic_json(path, {"status": "completed"})
    assert json.loads(path.read_bytes()) == {"status": "running"}
    assert not list(tmp_path.glob(".checkpoint-*"))


@respx.mock
@pytest.mark.parametrize("status", [404, 422])
def test_permanent_4xx_one_request(status):
    respx.post("https://api.avito.ru/token").respond(200, json={"access_token": "fake"})
    route = respx.get("https://api.avito.ru/core/v1/accounts/1/items/2/").respond(
        status, json={"error": "synthetic"}
    )
    with AvitoClient("client", "secret") as client:
        with pytest.raises(AvitoApiError) as error:
            client.get("/core/v1/accounts/1/items/2/")
    assert route.call_count == 1 and error.value.status_code == status


@respx.mock
def test_avito_retry_header(monkeypatch):
    delays = []
    monkeypatch.setattr("avito_raw_export.client.time.sleep", delays.append)
    respx.post("https://api.avito.ru/token").respond(200, json={"access_token": "fake"})
    respx.get("https://api.avito.ru/core/v1/accounts/self").mock(
        side_effect=[
            httpx.Response(429, headers={"X-RateLimit-Retry-After": "34"}),
            httpx.Response(200, json={"id": 1}),
        ]
    )
    with AvitoClient("client", "secret") as client:
        client.get("/core/v1/accounts/self")
    assert delays == [34]


@respx.mock
def test_streaming_without_response_body_buffer(tmp_path):
    class Chunks(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(10):
                yield b"x" * 262144

    respx.get("https://cdn.avito.ru/large.mp4").mock(
        return_value=httpx.Response(200, stream=Chunks())
    )
    path = tmp_path / "large.part"
    with AvitoClient("client", "secret") as client:
        response = client.download_to("https://cdn.avito.ru/large.mp4", path)
    assert response.content == b"" and path.stat().st_size == 10 * 262144


def test_live_lock_prevents_resume(tmp_path):
    first = make(tmp_path, FakeClient())
    first.run()
    with FileLock(str(first.store.root / ".resume.lock")):
        with pytest.raises(Timeout):
            make(tmp_path, FakeClient(), first.store.root).run()


def test_v1_running_archive_imports_without_redownload(tmp_path):
    first = make(tmp_path, FakeClient())
    first.run()
    root = first.store.root
    manifest = json.loads((root / "manifest.json").read_bytes())
    manifest.update(format="avito-raw-export-v1", status="running")
    manifest.pop("stages")
    manifest.pop("options")
    manifest.pop("legacy_counts", None)
    manifest["counts"]["errors"] = 99  # synthetic legacy event count
    atomic_json(root / "manifest.json", manifest)
    (root / "index/recovery.sqlite3").unlink()
    client = FakeClient()
    resumed = make(tmp_path, client, root)
    resumed.run()
    assert not any(method == "DOWNLOAD" for method, _, _ in client.calls)
    assert all(path == "/core/v1/accounts/self" for method, path, _ in client.calls)
    result = json.loads((root / "manifest.json").read_bytes())
    assert result["legacy_counts"]["errors"] == 99
    assert result["counts"]["errors"] == 0
    assert result["status"] == "completed"
    assert (root / "manifest.before-v02.json").exists()


def test_windows_checkpoint_sharing_violation_retries(tmp_path, monkeypatch):
    import avito_raw_export.store as module

    original = module.os.replace
    calls = []

    def sharing_lock(source, destination):
        calls.append(1)
        if len(calls) == 1:
            raise PermissionError("synthetic Windows sharing violation")
        return original(source, destination)

    monkeypatch.setattr(module.os, "replace", sharing_lock)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    atomic_json(tmp_path / "manifest.json", {"status": "running"})
    assert len(calls) == 2
    assert json.loads((tmp_path / "manifest.json").read_bytes())["status"] == "running"
