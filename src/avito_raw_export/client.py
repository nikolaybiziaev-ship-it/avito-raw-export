from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

API_BASE = "https://api.avito.ru"


class AvitoApiError(RuntimeError):
    def __init__(
        self, message: str, *, status_code: int | None = None, body: str | None = None
    ):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


@dataclass(slots=True)
class RawResponse:
    method: str
    url: str
    params: Any
    status_code: int
    headers: dict[str, str]
    content: bytes

    def json(self) -> Any:
        return json.loads(self.content.decode("utf-8"))


class AvitoClient:
    """Minimal read-only client. It intentionally exposes raw responses."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        timeout: float = 30.0,
        on_request: Callable[[RawResponse], None] | None = None,
    ) -> None:
        self.client_id = client_id.strip()
        self.client_secret = client_secret.strip()
        self.on_request = on_request
        self._token: str | None = None
        self._http = httpx.Client(
            base_url=API_BASE,
            timeout=timeout,
            follow_redirects=False,
            headers={"User-Agent": "avito-raw-export/0.1"},
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> AvitoClient:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def authenticate(self) -> dict[str, Any]:
        for attempt in range(5):
            try:
                response = self._http.post(
                    "/token",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                    },
                )
            except httpx.TransportError:
                if attempt == 4:
                    raise AvitoApiError("OAuth network error after retries") from None
                time.sleep(min(2**attempt, 20))
                continue
            if (
                response.status_code == 429 or response.status_code >= 500
            ) and attempt < 4:
                time.sleep(min(2**attempt, 20))
                continue
            break
        # OAuth responses contain credentials and must never enter export callbacks.
        if response.status_code >= 300:
            raise AvitoApiError(
                f"Ошибка авторизации Avito API: HTTP {response.status_code}",
                status_code=response.status_code,
                body=None,
            )
        try:
            payload = response.json()
        except ValueError:
            raise AvitoApiError("Invalid OAuth JSON response") from None
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise AvitoApiError("Avito API не вернул access_token", body=None)
        self._token = token
        return payload

    def get(self, path: str, *, params: Any = None, attempts: int = 5) -> RawResponse:
        allowed = (
            r"/core/v1/accounts/self",
            r"/core/v1/items",
            r"/core/v1/accounts/\d+/items/\d+/",
            r"/ratings/v1/(info|reviews)",
            r"/messenger/v2/accounts/\d+/chats(?:/[A-Za-z0-9_-]+)?",
            r"/messenger/v3/accounts/\d+/chats/[A-Za-z0-9_-]+/messages/",
            r"/messenger/v1/accounts/\d+/getVoiceFiles",
        )
        if not any(re.fullmatch(pattern, path) for pattern in allowed):
            raise ValueError("Endpoint is not in the read-only allowlist")
        if attempts < 1:
            raise ValueError("attempts must be positive")
        if not self._token:
            self.authenticate()
        headers = {"Authorization": f"Bearer {self._token}"}
        last: RawResponse | None = None
        refreshed = False
        for attempt in range(attempts):
            try:
                response = self._http.get(path, params=params, headers=headers)
            except httpx.TransportError:
                if attempt == attempts - 1:
                    raise AvitoApiError("Network error after retries") from None
                time.sleep(min(2**attempt, 20))
                continue
            raw = self._raw("GET", response, params=params)
            last = raw
            if response.status_code == 401 and not refreshed and attempt < attempts - 1:
                refreshed = True
                self.authenticate()
                headers = {"Authorization": f"Bearer {self._token}"}
                continue
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = (
                        min(max(float(retry_after), 1.0), 60.0)
                        if retry_after
                        else min(2**attempt, 30)
                    )
                except ValueError:
                    delay = min(2**attempt, 30)
                time.sleep(delay)
                continue
            if response.status_code >= 500:
                time.sleep(min(2**attempt, 20))
                continue
            if response.status_code >= 300:
                raise AvitoApiError(
                    f"Avito API: HTTP {response.status_code} для GET {path}",
                    status_code=response.status_code,
                    body=None,
                )
            return raw
        assert last is not None
        raise AvitoApiError(
            f"Avito API не ответил успешно после {attempts} попыток: GET {path}",
            status_code=last.status_code,
            body=last.content.decode("utf-8", errors="replace"),
        )

    def download(self, url: str, *, attempts: int = 3) -> RawResponse:
        if attempts < 1:
            raise ValueError("attempts must be positive")
        if not is_avito_media_url(url):
            raise ValueError("Untrusted media URL")
        last: RawResponse | None = None
        with httpx.Client(
            timeout=60.0,
            follow_redirects=False,
            headers={"User-Agent": "avito-raw-export/0.1"},
        ) as client:
            for attempt in range(attempts):
                try:
                    response = client.get(url)
                    for _ in range(5):
                        if not response.is_redirect:
                            break
                        target = str(
                            response.url.join(response.headers.get("location", ""))
                        )
                        if not is_avito_media_url(target):
                            raise ValueError("Untrusted media redirect")
                        response = client.get(target)
                except httpx.TransportError:
                    if attempt == attempts - 1:
                        raise AvitoApiError(
                            "Media network error after retries"
                        ) from None
                    time.sleep(min(2**attempt, 10))
                    continue
                raw = self._raw("GET", response, params=None, notify=False)
                last = raw
                if response.status_code == 429 or response.status_code >= 500:
                    time.sleep(min(2**attempt, 10))
                    continue
                if response.status_code >= 300:
                    raise AvitoApiError(
                        f"Не удалось скачать файл: HTTP {response.status_code}",
                        status_code=response.status_code,
                        body=response.text[:1000],
                    )
                return raw
        assert last is not None
        raise AvitoApiError(
            "Не удалось скачать файл после повторов", status_code=last.status_code
        )

    def _raw(
        self, method: str, response: httpx.Response, *, params: Any, notify: bool = True
    ) -> RawResponse:
        raw = RawResponse(
            method=method,
            url=str(response.request.url),
            params=params,
            status_code=response.status_code,
            headers=dict(response.headers),
            content=response.content,
        )
        if notify and self.on_request:
            self.on_request(raw)
        return raw


def is_avito_media_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        return (
            parsed.scheme == "https"
            and not parsed.username
            and not parsed.password
            and parsed.port in (None, 443)
            and any(
                host == domain or host.endswith("." + domain)
                for domain in ("avito.ru", "avito.st", "avcdn.net", "avcdn.ru")
            )
        )
    except ValueError:
        return False
