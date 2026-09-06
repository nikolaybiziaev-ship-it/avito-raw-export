from __future__ import annotations

import hashlib
import json
import mimetypes
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .client import AvitoApiError, AvitoClient, RawResponse, is_avito_media_url
from .store import ExportStore

ITEM_STATUSES = ("active", "removed", "old", "blocked", "rejected")
CHAT_TYPES = (None, "u2i", "u2u", "a2u")


@dataclass(slots=True)
class ExportOptions:
    export_root: Path
    download_voice: bool = True
    download_avito_media: bool = True
    list_items: bool = True
    item_details: bool = True
    global_chats: bool = True
    chats_by_item: bool = True
    chat_details: bool = True
    messages: bool = True
    ratings_and_reviews: bool = True


@dataclass(slots=True)
class ExportStats:
    account_id: int | None = None
    items: int = 0
    chats: int = 0
    messages_seen: int = 0
    reviews_seen: int = 0
    review_pages: int = 0
    message_pages: int = 0
    media_files: int = 0
    errors: int = 0
    oldest_message: int | None = None
    newest_message: int | None = None
    stage: str = ""
    detail: str = ""


class Exporter:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        options: ExportOptions,
        *,
        on_progress: Callable[[ExportStats], None] | None = None,
        on_log: Callable[[str], None] | None = None,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.options = options
        self.on_progress = on_progress or (lambda _: None)
        self.on_log = on_log or (lambda _: None)
        self.stats = ExportStats()
        self.store = ExportStore(options.export_root)
        self.client = AvitoClient(
            client_id, client_secret, on_request=self.store.record_request
        )
        self.item_ids: set[int] = set()
        self.chat_ids: set[str] = set()
        self.voice_ids: set[str] = set()
        self.media_urls: set[str] = set()
        self.review_ids: set[int] = set()
        self._detailed_item_ids: set[int] = set()
        self._items_scanned_for_chats: set[int] = set()
        self._detailed_chat_ids: set[str] = set()
        self._page_fingerprints: dict[str, set[str]] = {}

    def close(self) -> None:
        self.client.close()

    def run(self) -> Path:
        try:
            self._stage("connection", "Проверяю подключение")
            self.client.authenticate()
            account = self._get_account()
            account_id = self._extract_account_id(account)
            if account_id is None:
                raise RuntimeError(
                    "Не удалось определить user_id из /core/v1/accounts/self"
                )
            self.stats.account_id = account_id
            self.store.rename_for_account(account_id)
            self.store.save_json("account.json", account)
            self._log(f"✓ Подключен аккаунт Avito {account_id}")

            if self.options.list_items:
                self._discover_items()
            # Reviews may reference old item IDs missing from the generic item listing.
            # Export them early so those item IDs can also be used to discover extra chats.
            if self.options.ratings_and_reviews:
                self._export_ratings_and_reviews()
            if self.options.item_details:
                self._export_item_details()
            if self.options.global_chats:
                self._discover_global_chats()
            if self.options.chats_by_item:
                self._discover_chats_by_item()
            # Chat context can reveal an item that the generic items endpoint did not return.
            # Fetch details for those newly discovered item IDs too.
            if self.options.item_details:
                self._export_item_details()
            self.store.save_json("discovered_item_ids.json", sorted(self.item_ids))
            self.store.save_json("discovered_chat_ids.json", sorted(self.chat_ids))

            while True:
                before = (len(self.item_ids), len(self.chat_ids))
                if self.options.chat_details:
                    self._export_chat_details()
                if self.options.chats_by_item:
                    self._discover_chats_by_item()
                if self.options.item_details:
                    self._export_item_details()
                if before == (len(self.item_ids), len(self.chat_ids)):
                    break
            self.store.save_json("discovered_item_ids.json", sorted(self.item_ids))
            self.store.save_json("discovered_chat_ids.json", sorted(self.chat_ids))
            if self.options.messages:
                self._export_messages()
            if self.options.download_voice:
                self._download_voice_files()
            if self.options.download_avito_media:
                self._download_media_urls()

            self.store.save_json("media_urls.json", sorted(self.media_urls))
            self.store.manifest["status"] = (
                "partial" if self.stats.errors else "completed"
            )
            self._sync_manifest()
            self._stage("done", "Выгрузка завершена")
            self._log("✓ Выгрузка завершена")
            return self.store.root
        except Exception as exc:
            self.store.manifest["status"] = "failed"
            self._error("fatal", exc)
            raise
        finally:
            self.close()

    def _get_account(self) -> dict[str, Any]:
        raw = self.client.get("/core/v1/accounts/self")
        self.store.save_raw("account/self.json", raw)
        payload = self._json_object(raw)
        self._collect_media_urls(payload)
        return payload

    @staticmethod
    def _extract_account_id(payload: dict[str, Any]) -> int | None:
        candidates = [
            payload.get("id"),
            payload.get("user_id"),
            payload.get("userId"),
            (payload.get("result") or {}).get("id")
            if isinstance(payload.get("result"), dict)
            else None,
        ]
        for value in candidates:
            try:
                if value is not None:
                    return int(value)
            except (TypeError, ValueError):
                pass
        return None

    def _discover_items(self) -> None:
        self._stage("items", "Получаю объявления во всех доступных статусах")
        for status in ITEM_STATUSES:
            page = 1
            use_per_page = True
            while page <= 10000:
                params = {"status": status, "page": page}
                if use_per_page:
                    params["per_page"] = 100
                try:
                    raw = self.client.get("/core/v1/items", params=params)
                except AvitoApiError as exc:
                    # Some API revisions may reject per_page. Retry with the minimum known contract.
                    if page == 1 and exc.status_code == 400:
                        try:
                            use_per_page = False
                            raw = self.client.get(
                                "/core/v1/items",
                                params={"status": status, "page": page},
                            )
                        except Exception as second:
                            self._error(
                                "items-list", second, {"status": status, "page": page}
                            )
                            break
                    else:
                        self._error("items-list", exc, {"status": status, "page": page})
                        break
                except Exception as exc:
                    self._error("items-list", exc, {"status": status, "page": page})
                    break
                self.store.save_raw(f"items/lists/{status}/page_{page:05d}.json", raw)
                payload = self._json_object(raw)
                resources = self._list(payload, "resources")
                if self._repeated_page("items/" + status, resources):
                    break
                for row in resources:
                    if isinstance(row, dict):
                        item_id = self._coerce_int(
                            row.get("id") or row.get("item_id") or row.get("itemId")
                        )
                        if item_id is not None:
                            self.item_ids.add(item_id)
                        self._collect_media_urls(row)
                self.stats.items = len(self.item_ids)
                self._notify()
                self._log(
                    f"✓ {status}: страница {page}, получено {len(resources)} объявлений"
                )

                meta = (
                    payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
                )
                returned_page = self._coerce_int(meta.get("page")) or page
                if not resources:
                    break
                if returned_page != page and page > 1:
                    self._error(
                        "items-pagination",
                        RuntimeError("Server ignored page parameter"),
                    )
                    break
                page += 1
            else:
                self._error(
                    "items-pagination", RuntimeError("Page safety limit reached")
                )

    def _export_item_details(self) -> None:
        if not self.item_ids or self.stats.account_id is None:
            return
        self._stage("item_details", "Получаю карточки найденных объявлений")
        pending = [
            item_id
            for item_id in sorted(self.item_ids)
            if item_id not in self._detailed_item_ids
        ]
        total = len(pending)
        for idx, item_id in enumerate(pending, 1):
            try:
                raw = self.client.get(
                    f"/core/v1/accounts/{self.stats.account_id}/items/{item_id}/"
                )
                self.store.save_raw(f"items/details/{item_id}.json", raw)
                self._detailed_item_ids.add(item_id)
                self._collect_media_urls(self._json_any(raw))
            except Exception as exc:
                self._error("item-detail", exc, {"item_id": item_id})
            self.stats.detail = f"Карточка объявления {idx}/{total}"
            self._notify()

    def _discover_global_chats(self) -> None:
        if self.stats.account_id is None:
            return
        self._stage("chats_global", "Ищу чаты глобальным списком")
        for chat_type in CHAT_TYPES:
            label = chat_type or "all"
            self._walk_chat_pages(
                base_params=[] if chat_type is None else [("chat_types", chat_type)],
                raw_prefix=f"chats/lists/global/{label}",
                log_prefix=f"Глобальные чаты {label}",
            )

    def _discover_chats_by_item(self) -> None:
        if self.stats.account_id is None or not self.item_ids:
            return
        self._stage("chats_by_item", "Ищу дополнительные чаты через каждое объявление")
        # Iterate to a fixpoint: chat context may reveal additional item IDs.
        while True:
            pending = [
                item_id
                for item_id in sorted(self.item_ids)
                if item_id not in self._items_scanned_for_chats
            ]
            if not pending:
                break
            total = len(pending)
            for idx, item_id in enumerate(pending, 1):
                self._walk_chat_pages(
                    base_params=[("item_ids", str(item_id))],
                    raw_prefix=f"chats/lists/by_item/{item_id}",
                    log_prefix=f"Объявление {item_id}",
                    quiet=True,
                )
                self._items_scanned_for_chats.add(item_id)
                self.stats.detail = (
                    f"Объявление {idx}/{total}; найдено чатов {len(self.chat_ids)}"
                )
                self._notify()

    def _walk_chat_pages(
        self,
        *,
        base_params: list[tuple[str, str]],
        raw_prefix: str,
        log_prefix: str,
        quiet: bool = False,
    ) -> None:
        if self.stats.account_id is None:
            return
        offset = 0
        while offset <= 1000:
            params = [*base_params, ("limit", "100"), ("offset", str(offset))]
            try:
                raw = self.client.get(
                    f"/messenger/v2/accounts/{self.stats.account_id}/chats",
                    params=params,
                )
            except Exception as exc:
                self._error("chat-list", exc, {"params": params, "prefix": raw_prefix})
                return
            self.store.save_raw(f"{raw_prefix}/offset_{offset:04d}.json", raw)
            payload = self._json_object(raw)
            chats = self._list(payload, "chats")
            if self._repeated_page(raw_prefix, chats):
                break
            for chat in chats:
                if not isinstance(chat, dict):
                    continue
                chat_id = chat.get("id")
                if chat_id:
                    if re.fullmatch(r"[A-Za-z0-9_-]+", str(chat_id)):
                        self.chat_ids.add(str(chat_id))
                    else:
                        self._error("chat-id", ValueError("Unsafe chat identifier"))
                item_id = self._extract_item_id_from_chat(chat)
                if item_id is not None:
                    self.item_ids.add(item_id)
                self._collect_media_urls(chat)
            self.stats.chats = len(self.chat_ids)
            self.stats.items = len(self.item_ids)
            self._notify()
            if not quiet:
                self._log(f"✓ {log_prefix}: offset {offset}, получено {len(chats)}")
            if not chats:
                break
            offset += len(chats)
        else:
            self._error(
                "chat-pagination",
                RuntimeError("API offset ceiling reached; more chats may exist"),
                {"prefix": raw_prefix},
            )

    def _export_ratings_and_reviews(self) -> None:
        """Export seller rating and every review page exactly as returned by Avito."""
        self._stage("ratings", "Получаю рейтинг и все доступные отзывы")

        try:
            raw = self.client.get("/ratings/v1/info")
            self.store.save_raw("ratings/info.json", raw)
            self._collect_media_urls(self._json_any(raw))
            self._log("✓ Информация о рейтинге сохранена")
        except Exception as exc:
            # Ratings access can be unavailable independently of Messenger access.
            # Do not abort the rest of a maximum raw export.
            self._error("ratings-info", exc)

        limit = 50
        offset = 0
        max_pages = 100000
        for page_no in range(max_pages):
            try:
                raw = self.client.get(
                    "/ratings/v1/reviews",
                    params={"offset": offset, "limit": limit},
                )
            except Exception as exc:
                self._error("reviews-list", exc, {"offset": offset, "limit": limit})
                break

            self.store.save_raw(f"ratings/reviews/offset_{offset:07d}.json", raw)
            payload = self._json_object(raw)
            reviews = self._list(payload, "reviews")
            if self._repeated_page("reviews", reviews):
                break

            for review in reviews:
                if not isinstance(review, dict):
                    continue
                review_id = self._coerce_int(review.get("id"))
                if review_id is not None:
                    self.review_ids.add(review_id)
                item = review.get("item")
                if isinstance(item, dict):
                    item_id = self._coerce_int(
                        item.get("id") or item.get("item_id") or item.get("itemId")
                    )
                    if item_id is not None:
                        self.item_ids.add(item_id)
                self._collect_media_urls(review)
                self._collect_review_image_urls(review)

            self.stats.review_pages += 1
            self.stats.reviews_seen += len(reviews)
            self.stats.items = len(self.item_ids)
            self.stats.detail = f"Отзывы: offset {offset}; получено {len(reviews)}; всего {len(self.review_ids) or self.stats.reviews_seen}"
            self._notify()

            total = self._coerce_int(payload.get("total"))
            self._log(f"✓ Отзывы: offset {offset}, получено {len(reviews)}")
            if not reviews:
                break
            offset += len(reviews)
            if total is not None and offset >= total:
                break
        else:
            self._error("reviews-pagination", RuntimeError("Page safety limit reached"))

        self.store.save_json("review_ids.json", sorted(self.review_ids))

    def _collect_review_image_urls(self, review: dict[str, Any]) -> None:
        """Review image URLs are nested under images[].sizes[].url; preserve/download Avito-hosted ones."""
        images = review.get("images")
        if not isinstance(images, list):
            return
        for image in images:
            if not isinstance(image, dict):
                continue
            sizes = image.get("sizes")
            if not isinstance(sizes, list):
                continue
            for size in sizes:
                if not isinstance(size, dict):
                    continue
                url = size.get("url")
                if not isinstance(url, str) or not url.startswith(
                    ("http://", "https://")
                ):
                    continue
                if is_avito_media_url(url):
                    self.media_urls.add(url)

    def _export_chat_details(self) -> None:
        if self.stats.account_id is None:
            return
        self._stage("chat_details", "Получаю полные объекты чатов")
        total = len(self.chat_ids)
        for idx, chat_id in enumerate(
            sorted(self.chat_ids - self._detailed_chat_ids), 1
        ):
            self._detailed_chat_ids.add(chat_id)
            try:
                raw = self.client.get(
                    f"/messenger/v2/accounts/{self.stats.account_id}/chats/{chat_id}"
                )
                self.store.save_raw(f"chats/details/{chat_id}.json", raw)
                payload = self._json_object(raw)
                item_id = self._extract_item_id_from_chat(payload)
                if item_id is not None:
                    self.item_ids.add(item_id)
                self._collect_media_urls(payload)
            except Exception as exc:
                self._error("chat-detail", exc, {"chat_id": chat_id})
            self.stats.detail = f"Чат {idx}/{total}"
            self._notify()

    def _export_messages(self) -> None:
        if self.stats.account_id is None:
            return
        self._stage("messages", "Получаю историю сообщений каждого чата")
        total = len(self.chat_ids)
        for chat_idx, chat_id in enumerate(sorted(self.chat_ids), 1):
            offset = 0
            while offset <= 1000:
                try:
                    raw = self.client.get(
                        f"/messenger/v3/accounts/{self.stats.account_id}/chats/{chat_id}/messages/",
                        params={"limit": 100, "offset": offset},
                    )
                except Exception as exc:
                    self._error("messages", exc, {"chat_id": chat_id, "offset": offset})
                    break
                self.store.save_raw(f"messages/{chat_id}/offset_{offset:04d}.json", raw)
                payload = self._json_any(raw)
                messages = (
                    payload
                    if isinstance(payload, list)
                    else self._list(payload, "messages")
                )
                if self._repeated_page("messages/" + chat_id, messages):
                    break
                self.stats.message_pages += 1
                self.stats.messages_seen += len(messages)
                for message in messages:
                    if isinstance(message, dict):
                        self._observe_message(message)
                self.stats.detail = f"Чат {chat_idx}/{total}; offset {offset}; сообщений {self.stats.messages_seen}"
                self._notify()
                if not messages:
                    break
                offset += len(messages)
            else:
                self._error(
                    "message-pagination",
                    RuntimeError(
                        "API offset ceiling reached; older messages may exist"
                    ),
                    {"chat_id": chat_id},
                )
            if chat_idx % 25 == 0 or chat_idx == total:
                self._log(f"✓ Обработано чатов с сообщениями: {chat_idx}/{total}")
        self.store.save_json("voice_ids.json", sorted(self.voice_ids))
        self.store.save_json("media_urls.json", sorted(self.media_urls))

    def _observe_message(self, message: dict[str, Any]) -> None:
        ts = self._coerce_int(message.get("created"))
        if ts is not None:
            self.stats.oldest_message = (
                ts
                if self.stats.oldest_message is None
                else min(self.stats.oldest_message, ts)
            )
            self.stats.newest_message = (
                ts
                if self.stats.newest_message is None
                else max(self.stats.newest_message, ts)
            )
        content = message.get("content")
        if isinstance(content, dict):
            voice = content.get("voice")
            if isinstance(voice, dict) and voice.get("voice_id"):
                self.voice_ids.add(str(voice["voice_id"]))
        self._collect_media_urls(message)

    def _download_voice_files(self) -> None:
        if self.stats.account_id is None or not self.voice_ids:
            return
        self._stage("voice", "Получаю ссылки и скачиваю голосовые сообщения")
        ids = sorted(self.voice_ids)
        # Small batches keep query strings reasonable. API accepts voice_ids list.
        for batch_no in range(0, len(ids), 20):
            batch = ids[batch_no : batch_no + 20]
            params = [("voice_ids", voice_id) for voice_id in batch]
            try:
                raw = self.client.get(
                    f"/messenger/v1/accounts/{self.stats.account_id}/getVoiceFiles",
                    params=params,
                )
                self.store.save_raw(f"voice/links/batch_{batch_no // 20:05d}.json", raw)
                payload = self._json_object(raw)
                urls = payload.get("voices_urls") or payload.get("voicesUrls") or {}
                if not isinstance(urls, dict):
                    raise ValueError("Expected voice URL mapping")
                missing = set(batch) - {str(key) for key in urls}
                if missing:
                    self._error(
                        "voice-links",
                        ValueError("API omitted requested voice files"),
                        {"voice_ids": sorted(missing)},
                    )
                if isinstance(urls, dict):
                    for voice_id, value in urls.items():
                        url = self._url_from_voice_value(value)
                        if not url:
                            self._error(
                                "voice-links",
                                ValueError("Missing voice URL"),
                                {"voice_id": voice_id},
                            )
                            continue
                        try:
                            file_raw = self.client.download(url)
                            ext = self._extension(file_raw, url, default=".bin")
                            self.store.save_media(
                                "voice",
                                f"{voice_id}{ext}",
                                file_raw.content,
                                raw=file_raw,
                            )
                        except Exception as exc:
                            self._error(
                                "voice-download",
                                exc,
                                {"voice_id": voice_id, "url": url},
                            )
            except Exception as exc:
                self._error("voice-links", exc, {"voice_ids": batch})
            self.stats.media_files = self.store.manifest["counts"]["media_files"]
            self._notify()

    def _download_media_urls(self) -> None:
        urls = sorted(self.media_urls)
        if not urls:
            return
        self._stage("media", "Скачиваю доступные медиафайлы Avito")
        seen_hashes: set[str] = set()
        for idx, url in enumerate(urls, 1):
            try:
                raw = self.client.download(url)
                digest = __import__("hashlib").sha256(raw.content).hexdigest()
                if digest in seen_hashes:
                    continue
                seen_hashes.add(digest)
                parsed = urlparse(url)
                base = Path(parsed.path).name or f"media_{idx}"
                if "." not in base:
                    base += self._extension(raw, url, default=".bin")
                self.store.save_media(
                    "avito", f"{idx:06d}_{base}", raw.content, raw=raw
                )
            except Exception as exc:
                self._error("media-download", exc, {"url": url})
            self.stats.media_files = self.store.manifest["counts"]["media_files"]
            self.stats.detail = f"Медиа {idx}/{len(urls)}"
            self._notify()

    def _collect_media_urls(self, value: Any, *, key: str = "") -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                self._collect_media_urls(v, key=key + "." + str(k).lower())
        elif isinstance(value, list):
            for v in value:
                self._collect_media_urls(v, key=key)
        elif isinstance(value, str) and value.startswith(("http://", "https://")):
            # Download only URLs that look like Avito-hosted media. External links are preserved in raw JSON only.
            if is_avito_media_url(value) and self._looks_like_media_key_or_url(
                key, value
            ):
                self.media_urls.add(value)

    @staticmethod
    def _looks_like_media_key_or_url(key: str, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        if "img.avito" in host or "avcdn" in host:
            return True
        media_keys = ("image", "avatar", "photo", "file", "video", "preview")
        if any(part in key for part in media_keys):
            return True
        path = urlparse(url).path.lower()
        return any(
            path.endswith(ext)
            for ext in (
                ".jpg",
                ".jpeg",
                ".png",
                ".webp",
                ".gif",
                ".mp4",
                ".opus",
                ".ogg",
                ".pdf",
            )
        )

    @staticmethod
    def _url_from_voice_value(value: Any) -> str | None:
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            for key in ("url", "file_url", "fileUrl"):
                if isinstance(value.get(key), str):
                    return value[key]
        return None

    @staticmethod
    def _extension(raw: RawResponse, url: str, default: str = ".bin") -> str:
        content_type = (
            raw.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        )
        ext = mimetypes.guess_extension(content_type) if content_type else None
        if ext:
            return ext
        suffix = Path(urlparse(url).path).suffix
        return suffix if suffix and len(suffix) <= 8 else default

    @staticmethod
    def _extract_item_id_from_chat(chat: dict[str, Any]) -> int | None:
        context = chat.get("context")
        if isinstance(context, dict):
            value = context.get("value")
            if isinstance(value, dict):
                result = Exporter._coerce_int(
                    value.get("id") or value.get("item_id") or value.get("itemId")
                )
                if result is not None:
                    return result
        return Exporter._coerce_int(chat.get("item_id") or chat.get("itemId"))

    def _json_any(self, raw: RawResponse) -> Any:
        try:
            return raw.json()
        except (ValueError, UnicodeError):
            self._error("response-format", ValueError("Response is not valid JSON"))
            return None

    def _json_object(self, raw: RawResponse) -> dict[str, Any]:
        payload = self._json_any(raw)
        if not isinstance(payload, dict):
            self._error("response-format", ValueError("Expected JSON object"))
            return {}
        return payload

    def _list(self, payload: Any, key: str) -> list:
        value = payload.get(key) if isinstance(payload, dict) else None
        if not isinstance(value, list):
            self._error("response-format", ValueError(f"Expected list field: {key}"))
            return []
        if any(not isinstance(row, dict) for row in value):
            self._error("response-format", ValueError(f"Invalid row in {key}"))
        return value

    def _repeated_page(self, key: str, rows: list) -> bool:
        if not rows:
            return False
        fingerprint = hashlib.sha256(
            json.dumps(rows, sort_keys=True).encode()
        ).hexdigest()
        seen = self._page_fingerprints.setdefault(key, set())
        if fingerprint in seen:
            self._error(
                "pagination",
                RuntimeError("Repeated page; export may be incomplete"),
                {"collection": key},
            )
            return True
        seen.add(fingerprint)
        return False

    @staticmethod
    def _coerce_int(value: Any) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _stage(self, stage: str, detail: str) -> None:
        self.stats.stage = stage
        self.stats.detail = detail
        self._notify()

    def _notify(self) -> None:
        self._sync_manifest()
        self.on_progress(self.stats)

    def _log(self, line: str) -> None:
        self.on_log(line)

    def _error(self, where: str, exc: Exception, context: Any = None) -> None:
        self.store.error(where, exc, context)
        self.stats.errors = self.store.manifest["counts"]["errors"]
        self.on_log(f"⚠ {where}: {exc}")
        self._notify()

    def _sync_manifest(self) -> None:
        counts = self.store.manifest["counts"]
        counts.update(
            {
                "items": len(self.item_ids),
                "chats": len(self.chat_ids),
                "message_pages": self.stats.message_pages,
                "messages_seen": self.stats.messages_seen,
                "review_pages": self.stats.review_pages,
                "reviews_seen": self.stats.reviews_seen,
                "unique_reviews": len(self.review_ids),
                "media_files": self.store.manifest["counts"].get("media_files", 0),
                "errors": self.store.manifest["counts"].get("errors", 0),
            }
        )
        self.store.manifest["oldest_message_timestamp"] = self.stats.oldest_message
        self.store.manifest["newest_message_timestamp"] = self.stats.newest_message
        self.store.write_manifest()
