"""Structured, deduplicated issues; private context stays inside the archive."""

import hashlib
import json

from .client import AvitoApiError


def classify(where, error):
    status = getattr(error, "status_code", None)
    body = getattr(error, "body", None) or ""
    try:
        body = json.dumps(json.loads(body), ensure_ascii=False)
    except (ValueError, TypeError):
        pass
    if (
        where == "item-detail"
        and status == 422
        and "Объявление не принадлежит пользователю" in body
    ):
        return "unavailable_by_api", "foreign_item", False
    if where == "statistics-window" and status == 403:
        return "no_permission", "http_403", False
    if where == "statistics-window" and status in (400, 422):
        return "malformed_request", f"http_{status}", True
    if status in (403, 404, 410):
        return "unavailable_by_api", f"http_{status}", False
    if (
        status == 429
        or (status is not None and status >= 500)
        or (isinstance(error, AvitoApiError) and status is None)
    ):
        return "transient_network", f"http_{status}" if status else "network", True
    if where in (
        "chat-pagination",
        "message-pagination",
        "items-pagination",
        "reviews-pagination",
    ):
        return "expected_limitations", "pagination_limit", False
    if where == "voice-missing":
        return "unavailable_by_api", "voice_missing", False
    if where in ("media-skipped", "identifier-skipped"):
        return "skipped_safely", "untrusted_value", False
    return "exporter_error", f"http_{status}" if status else type(error).__name__, True


def issue_key(where, context):
    return hashlib.sha256(
        json.dumps([where, context], sort_keys=True, default=str).encode()
    ).hexdigest()
