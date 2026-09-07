"""Read-only historical backfill for the public Avito Statistics API v2."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta


ITEM_METRICS = (
    "views",
    "contacts",
    "contactsShowPhone",
    "contactsMessenger",
    "contactsShowPhoneAndMessenger",
    "contactsSbcDiscount",
    "viewsToContactsConversion",
    "favorites",
    "averageViewCost",
    "averageContactCost",
    "impressions",
    "impressionsToViewsConversion",
    "clickPackages",
    "jobContacts",
    "viewsToOrderedItemsConversion",
    "orderedItems",
    "orderedItemsPrice",
    "deliveredItems",
    "deliveredItemsPrice",
    "bookingPlacedCount",
    "bookingPlacedPrice",
    "bookingApprovedCount",
    "bookingApprovedPrice",
    "bookingAcceptedCount",
    "bookingAcceptedPrice",
    "allSpending",
    "spending",
    "presenceSpending",
    "promoSpending",
    "restSpending",
    "commission",
    "spendingBonus",
    "activeItems",
    "newActiveItems",
    "oldActiveItems",
)

STATISTICS_MIN_INTERVAL_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class StatisticsWindow:
    source: str
    date_from: date
    date_to: date
    grouping: str

    @property
    def key(self) -> str:
        value = f"{self.source}|{self.date_from}|{self.date_to}|{self.grouping}"
        return hashlib.sha256(value.encode()).hexdigest()


def date_windows(end: date, horizon_days: int, window_days: int):
    """Return inclusive, oldest-first windows covering the documented horizon."""
    start = end - timedelta(days=horizon_days - 1)
    cursor = start
    while cursor <= end:
        finish = min(cursor + timedelta(days=window_days - 1), end)
        yield cursor, finish
        cursor = finish + timedelta(days=1)


class StatisticsBackfill:
    """Backfill exact daily item data, account daily data, and daily spendings."""

    def __init__(
        self,
        exporter,
        *,
        today: date | None = None,
        min_interval=STATISTICS_MIN_INTERVAL_SECONDS,
    ):
        self.exporter = exporter
        self.today = today or datetime.now(UTC).date()
        self.min_interval = min_interval
        self._last_request = 0.0

    def windows(self):
        # One-day windows with `item` retain per-item daily detail.
        for start, end in date_windows(self.today, 270, 1):
            yield StatisticsWindow("items_daily", start, end, "item")
        # `day` returns account-level timestamp groups. Larger request windows are safe
        # because the public contract gives a horizon, not a smaller date-range limit.
        for start, end in date_windows(self.today, 270, 90):
            yield StatisticsWindow("account_daily", start, end, "day")
        for start, end in date_windows(self.today, 270, 90):
            yield StatisticsWindow("spendings_daily", start, end, "day")

    def run(self):
        exp = self.exporter
        windows = list(self.windows())
        exp.store.save_json(
            "statistics/catalog.json",
            {
                "contract": "Avito Statistics API v2",
                "generated_at": datetime.now(UTC).isoformat(),
                "item_metrics": list(ITEM_METRICS),
                "item_horizon_days": 270,
                "spendings_horizon_days": 270,
                "request_limit_per_minute": 1,
                "windows": [
                    {
                        "key": w.key,
                        "source": w.source,
                        "date_from": str(w.date_from),
                        "date_to": str(w.date_to),
                        "grouping": w.grouping,
                    }
                    for w in windows
                ],
            },
        )
        exp._stage("statistics_discovery", f"Подготовлено периодов: {len(windows)}")
        pending = [w for w in windows if not exp.journal.statistics_done(w.key)]
        exp._stage(
            "statistics_backfill",
            f"Периодов осталось: {len(pending)} из {len(windows)}",
        )
        blocked_endpoints = set()
        failures = []
        for position, window in enumerate(pending, 1):
            endpoint = self._endpoint(window)
            if endpoint in blocked_endpoints:
                continue
            exp._check_stop()
            exp.stats.detail = (
                f"Статистика: {window.date_from} — {window.date_to}; "
                f"период {position}/{len(pending)}"
            )
            exp._notify()
            try:
                records = self._fetch_window(window)
                exp.journal.statistics_commit(
                    window.key,
                    endpoint,
                    str(window.date_from),
                    str(window.date_to),
                    records,
                )
                exp.journal.resolve("statistics-window", {"key": window.key})
                exp._sync_manifest()
            except Exception as exc:
                exp.journal.statistics_failed(
                    window.key,
                    self._endpoint(window),
                    str(window.date_from),
                    str(window.date_to),
                    exc,
                )
                exp._error(
                    "statistics-window",
                    exc,
                    {
                        "key": window.key,
                        "source": window.source,
                        "date_from": str(window.date_from),
                        "date_to": str(window.date_to),
                    },
                )
                # One contract/permission/network failure is enough evidence to stop
                # this endpoint family for the run. Resume retries its first missing
                # window later and avoids hundreds of redundant failing requests.
                blocked_endpoints.add(endpoint)
                failures.append(exc)

        periods, records = exp.journal.statistics_counts()
        complete = all(exp.journal.statistics_done(w.key) for w in windows)
        status = "completed" if complete else "partial"
        detail = (
            f"Статистика: {records} записей, обработано {periods} периодов"
            if complete
            else self._failure_summary(failures[0] if failures else None)
        )
        exp.stats.statistics_status = detail
        exp.store.manifest["statistics"] = {
            "status": status,
            "planned_periods": len(windows),
            "completed_periods": periods,
            "records": records,
            "failed_requests": len(failures),
            "skipped_periods": len(windows) - periods - len(failures),
            "reason": None if complete else detail,
        }
        exp._finish_stage(status, detail)
        exp._stage("statistics_done", detail)
        exp._finish_stage(status, detail)

    @staticmethod
    def _failure_summary(error):
        status = getattr(error, "status_code", None)
        if status == 403:
            return "Статистика недоступна: HTTP 403 — нет доступа"
        if status in (404, 410):
            return f"Статистика недоступна: HTTP {status} — метод недоступен"
        if status in (400, 422):
            return f"Статистика: ошибка запроса — HTTP {status}"
        if status == 429:
            return "Статистика временно недоступна: HTTP 429"
        if status is not None and status >= 500:
            return f"Статистика временно недоступна: HTTP {status}"
        if error is None:
            return "Статистика выполнена не полностью"
        if isinstance(error, ValueError):
            return "Статистика: некорректный ответ API"
        return "Статистика временно недоступна: ошибка сети"

    def _fetch_window(self, window):
        if window.source == "spendings_daily":
            return self._fetch_spendings(window)
        return self._fetch_items(window)

    def _endpoint(self, window):
        resource = "spendings" if window.source == "spendings_daily" else "items"
        return f"/stats/v2/accounts/{self.exporter.stats.account_id}/{resource}"

    def _fetch_items(self, window):
        endpoint = self._endpoint(window)
        records = []
        offset = 0
        while True:
            body = {
                "dateFrom": str(window.date_from),
                "dateTo": str(window.date_to),
                "grouping": window.grouping,
                "metrics": list(ITEM_METRICS),
                "limit": 1000,
                "offset": offset,
            }
            raw = self._post(endpoint, body)
            payload = self._payload(raw)
            result = payload.get("result")
            if not isinstance(result, dict):
                raise ValueError("Statistics response has no result object")
            groups = result.get("groupings")
            total = result.get("dataTotalCount")
            if not isinstance(groups, list) or not isinstance(total, int):
                raise ValueError("Malformed statistics groupings or total count")
            records.extend(self._item_records(groups))
            self._save_raw(window, raw, offset)
            offset += len(groups)
            if offset >= total:
                break
            if not groups:
                raise ValueError("Statistics pagination stopped before total count")
        return records

    def _fetch_spendings(self, window):
        endpoint = self._endpoint(window)
        body = {
            "dateFrom": str(window.date_from),
            "dateTo": str(window.date_to),
            "grouping": "day",
            "spendingTypes": ["all"],
        }
        raw = self._post(endpoint, body)
        payload = self._payload(raw)
        result = payload.get("result")
        groups = result.get("groupings") if isinstance(result, dict) else None
        if not isinstance(groups, list):
            raise ValueError("Malformed spendings response")
        records = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("spendings"), list):
                raise ValueError("Malformed spendings grouping")
            record_id = group.get("date")
            if not isinstance(record_id, str):
                raise ValueError("Spendings grouping has no date")
            for spending in group["spendings"]:
                if not isinstance(spending, dict) or "slug" not in spending or "value" not in spending:
                    raise ValueError("Malformed spending metric")
                records.append(self._record(group, record_id, "spending:" + str(spending["slug"]), spending["value"]))
                services = spending.get("services", [])
                if not isinstance(services, list):
                    raise ValueError("Malformed spending services")
                for service in services:
                    if not isinstance(service, dict) or "slug" not in service or "value" not in service:
                        raise ValueError("Malformed spending service")
                    metric = (
                        "service:"
                        + str(spending["slug"])
                        + ":"
                        + str(service["slug"])
                    )
                    records.append(
                        self._record(group, record_id, metric, service["value"])
                    )
        self._save_raw(window, raw, 0)
        return records

    def _item_records(self, groups):
        records = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("metrics"), list):
                raise ValueError("Malformed statistics grouping")
            if "id" not in group or not isinstance(group.get("type"), str):
                raise ValueError("Statistics grouping lacks id or type")
            for metric in group["metrics"]:
                if not isinstance(metric, dict) or "slug" not in metric or "value" not in metric:
                    raise ValueError("Malformed statistics metric")
                records.append(self._record(group, group["id"], str(metric["slug"]), metric["value"]))
        return records

    @staticmethod
    def _record(group, record_id, metric, value):
        return {
            "grouping_type": group.get("type", "day"),
            "record_id": record_id,
            "metric": metric,
            "value": value,
        }

    def _post(self, endpoint, body):
        now = time.monotonic()
        remaining = self.min_interval - (now - self._last_request)
        if remaining > 0:
            time.sleep(remaining)
        try:
            return self.exporter.client.post(endpoint, json_body=body)
        finally:
            self._last_request = time.monotonic()

    @staticmethod
    def _payload(raw):
        try:
            payload = raw.json()
        except (ValueError, UnicodeError):
            raise ValueError("Statistics response is not valid JSON") from None
        if not isinstance(payload, dict):
            raise ValueError("Statistics response is not a JSON object")
        return payload

    def _save_raw(self, window, raw, offset):
        relative = (
            f"statistics/{window.source}/{window.date_from}_{window.date_to}/"
            f"offset_{offset:05d}.json"
        )
        self.exporter.store.save_raw(relative, raw)
