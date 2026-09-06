import hashlib
import json

import pytest

from avito_raw_export.client import RawResponse
from avito_raw_export.config import Profile, ProfileStore
from avito_raw_export.store import ExportStore


def test_unique_exports_raw_integrity_and_paths(tmp_path):
    store = ExportStore(tmp_path)
    assert store.root != ExportStore(tmp_path).root
    body = b'{ "unknown": [1,2], "unicode":"\\u0410" }\n'
    response = RawResponse(
        "GET", "/example", None, 200, {"Set-Cookie": "private"}, body
    )
    path = store.save_raw("example.json", response)
    assert path.read_bytes() == body
    meta = json.loads(path.with_suffix(".json.meta.json").read_text())
    assert meta["sha256"] == hashlib.sha256(body).hexdigest()
    assert meta["headers"]["Set-Cookie"] == "<redacted>"
    with pytest.raises(ValueError):
        store.save_raw("../../outside.json", response)
    with pytest.raises(ValueError):
        store.save_json(str(tmp_path / "outside.json"), {})


def test_keyring_failure_does_not_save_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "avito_raw_export.config.user_config_dir", lambda _: str(tmp_path)
    )
    store = ProfileStore()

    def fail(*args):
        raise RuntimeError("backend unavailable")

    monkeypatch.setattr("avito_raw_export.config.keyring.set_password", fail)
    with pytest.raises(RuntimeError):
        store.save(Profile("test", "test-client"), "test-secret")
    assert not store.path.exists()


def test_multiple_profiles_and_corrupt_config(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "avito_raw_export.config.user_config_dir", lambda _: str(tmp_path)
    )
    secrets = {}
    monkeypatch.setattr(
        "avito_raw_export.config.keyring.set_password",
        lambda service, name, secret: secrets.update({name: secret}),
    )
    monkeypatch.setattr(
        "avito_raw_export.config.keyring.get_password",
        lambda service, name: secrets.get(name),
    )
    store = ProfileStore()
    store.save(Profile("one", "test-client-1"), "synthetic-secret-1")
    store.save(Profile("two", "test-client-2"), "synthetic-secret-2")
    assert len(store.list()) == 2
    assert store.secret("one") == "synthetic-secret-1"
    assert "synthetic-secret" not in store.path.read_text()
    store.path.write_text("broken")
    with pytest.raises(RuntimeError):
        store.save(Profile("three", "test-client-3"), "synthetic-secret-3")
    assert store.path.read_text() == "broken"
