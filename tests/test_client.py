import httpx
import pytest
import respx

from avito_raw_export.client import AvitoApiError, AvitoClient, is_avito_media_url
from avito_raw_export.store import ExportStore


@pytest.mark.parametrize(
    "path",
    [
        "https://evil.example/data",
        "//evil.example",
        "/token",
        "/messenger/v1/accounts/1/chats/x/read",
        "/ratings/v1/answers",
        "/messenger/v2/accounts/1/chats/../../read",
    ],
)
def test_reject_unapproved_paths(path):
    with AvitoClient("test-client", "test-secret") as client:
        with pytest.raises(ValueError):
            client.get(path)


@pytest.mark.parametrize(
    "url",
    [
        "http://cdn.avito.ru/a",
        "https://avito.ru.evil.example/a",
        "https://fakeavito.ru/a",
        "https://localhost/a",
        "https://127.0.0.1/a",
        "https://user:pass@cdn.avito.ru/a",
        "https://cdn.avito.ru:8080/a",
    ],
)
def test_reject_untrusted_media(url):
    assert not is_avito_media_url(url)


@respx.mock
def test_retries_refresh_raw_and_oauth_exclusion(tmp_path, monkeypatch):
    monkeypatch.setattr("avito_raw_export.client.time.sleep", lambda _: None)
    store = ExportStore(tmp_path)
    token = respx.post("https://api.avito.ru/token").mock(
        return_value=httpx.Response(
            200, json={"access_token": "synthetic-private-token"}
        )
    )
    route = respx.get("https://api.avito.ru/core/v1/accounts/self").mock(
        side_effect=[
            httpx.Response(401, content=b'{"error":"expired"}'),
            httpx.Response(429, headers={"Retry-After": "invalid"}, content=b"busy"),
            httpx.ConnectError("offline"),
            httpx.Response(503, content=b"unavailable"),
            httpx.Response(200, content=b'{ "id": 1, "unknown": [true] }'),
        ]
    )
    with AvitoClient(
        "test-client", "test-secret", on_request=store.record_request
    ) as client:
        response = client.get("/core/v1/accounts/self")
    assert response.content == b'{ "id": 1, "unknown": [true] }'
    assert token.call_count == 2 and route.call_count == 5
    raws = list(store.raw.glob("requests/*.json"))
    assert len([p for p in raws if not p.name.endswith(".meta.json")]) == 4
    assert all(
        b"synthetic-private-token" not in p.read_bytes()
        for p in store.root.rglob("*")
        if p.is_file()
    )


@respx.mock
def test_redirect_cannot_forward_oauth():
    respx.post("https://api.avito.ru/token").respond(
        307, headers={"Location": "https://evil.example"}
    )
    with AvitoClient("test-client", "test-secret") as client:
        with pytest.raises(AvitoApiError):
            client.authenticate()


@respx.mock
def test_media_redirect_is_checked():
    respx.get("https://cdn.avito.ru/a").respond(
        302, headers={"Location": "http://127.0.0.1/private"}
    )
    with AvitoClient("test-client", "test-secret") as client:
        with pytest.raises(ValueError):
            client.download("https://cdn.avito.ru/a")


@respx.mock
def test_transport_exhaustion(monkeypatch):
    monkeypatch.setattr("avito_raw_export.client.time.sleep", lambda _: None)
    respx.post("https://api.avito.ru/token").respond(200, json={"access_token": "fake"})
    route = respx.get("https://api.avito.ru/core/v1/accounts/self").mock(
        side_effect=httpx.ReadTimeout("timeout")
    )
    with AvitoClient("test-client", "test-secret") as client:
        with pytest.raises(AvitoApiError):
            client.get("/core/v1/accounts/self", attempts=2)
    assert route.call_count == 2


@respx.mock
def test_oauth_retry_and_late_401_refresh(monkeypatch):
    monkeypatch.setattr("avito_raw_export.client.time.sleep", lambda _: None)
    auth = respx.post("https://api.avito.ru/token").mock(
        side_effect=[
            httpx.ConnectError("offline"),
            httpx.Response(503),
            httpx.Response(200, json={"access_token": "fake-first"}),
            httpx.Response(200, json={"access_token": "fake-second"}),
        ]
    )
    route = respx.get("https://api.avito.ru/core/v1/accounts/self").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(401),
            httpx.Response(200, json={"id": 1}),
        ]
    )
    with AvitoClient("test-client", "test-secret") as client:
        assert client.get("/core/v1/accounts/self").json()["id"] == 1
    assert auth.call_count == 4
    assert route.calls[-1].request.headers["Authorization"] == "Bearer fake-second"
