from __future__ import annotations

import json
from datetime import date

import httpx
import pytest
import respx

from avito_raw_export.client import AvitoApiError, AvitoClient, RawResponse
from avito_raw_export.exporter import Exporter, ExportOptions
from avito_raw_export.recovery import Journal
from avito_raw_export.statistics import StatisticsBackfill, StatisticsWindow
from avito_raw_export.store import ExportStore
from test_exporter import FakeClient


def post_raw(path, payload, status=200, headers=None):
    return RawResponse(
        "POST",
        path,
        None,
        status,
        headers or {"content-type": "application/json"},
        json.dumps(payload).encode(),
    )


class StatsClient:
    def __init__(self, responder=None):
        self.calls = []
        self.on_request = None
        self.responder = responder or self._respond

    def authenticate(self):
        return {"access_token": "synthetic"}

    def close(self):
        pass

    def get(self, path, *, params=None, attempts=5):
        assert path == "/core/v1/accounts/self"
        return RawResponse("GET", path, params, 200, {}, b'{"id":777}')

    def post(self, path, *, json_body, attempts=5):
        self.calls.append((path, json_body))
        raw = self.responder(path, json_body)
        raw.params = json_body
        if self.on_request:
            self.on_request(raw)
        if raw.status_code >= 300:
            raise AvitoApiError(
                "synthetic failure",
                status_code=raw.status_code,
                body=raw.content.decode(),
            )
        return raw

    @staticmethod
    def _respond(path, body):
        if path.endswith("/spendings"):
            return post_raw(
                path,
                {
                    "result": {
                        "timestamp": 1,
                        "groupings": [
                            {
                                "date": body["dateFrom"],
                                "type": "day",
                                "spendings": [
                                    {
                                        "slug": "promotion",
                                        "value": 12.5,
                                        "services": [
                                            {"slug": "vas_xl", "value": 10.0}
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                },
            )
        record_id = 111 if body["grouping"] == "totals" else 1735689600
        return post_raw(
            path,
            {
                "result": {
                    "dataTotalCount": 1,
                    "groupings": [
                        {
                            "id": record_id,
                            "type": body["grouping"],
                            "metrics": [{"slug": "views", "value": 7}],
                        }
                    ],
                }
            },
        )


def options(tmp_path, *, statistics=True, resume_from=None):
    return ExportOptions(
        tmp_path,
        statistics=statistics,
        resume_from=resume_from,
        list_items=False,
        item_details=False,
        global_chats=False,
        chats_by_item=False,
        chat_details=False,
        messages=False,
        ratings_and_reviews=False,
    )


def run_stats(tmp_path, monkeypatch, windows, client=None, resume_from=None):
    monkeypatch.setattr(StatisticsBackfill, "windows", lambda self: iter(windows))
    monkeypatch.setattr("avito_raw_export.statistics.time.sleep", lambda _: None)
    exporter = Exporter("client", "secret", options(tmp_path, resume_from=resume_from))
    exporter.client.close()
    exporter.client = client or StatsClient()
    result = exporter.run()
    return exporter, result


def item_window(day="2025-01-01"):
    current = date.fromisoformat(day)
    return StatisticsWindow("items_daily", current, current, "totals")


def test_basic_export_without_media_or_voice_is_completed(tmp_path):
    exporter = Exporter("client", "secret", ExportOptions(tmp_path, statistics=False))
    exporter.client.close()
    fake = FakeClient()
    exporter.client = fake
    result = exporter.run()
    manifest = json.loads((result / "manifest.json").read_bytes())
    assert manifest["status"] == "completed"
    assert manifest["counts"]["media_files"] == 0
    assert not any(call[0] == "DOWNLOAD" for call in fake.calls)
    assert "voice" not in manifest["stages"] and "media" not in manifest["stages"]


def test_statistics_single_window_and_normalized_record(tmp_path, monkeypatch):
    exporter, result = run_stats(tmp_path, monkeypatch, [item_window()])
    assert exporter.stats.statistics_periods == 1
    assert exporter.stats.statistics_records == 1
    assert list((result / "raw/statistics/items_daily").rglob("offset_00000.json"))
    assert json.loads((result / "manifest.json").read_bytes())["status"] == "completed"


def test_statistics_multiple_windows(tmp_path, monkeypatch):
    windows = [item_window("2025-01-01"), item_window("2025-01-02")]
    client = StatsClient()
    exporter, _ = run_stats(tmp_path, monkeypatch, windows, client)
    assert len(client.calls) == 2
    assert exporter.stats.statistics_periods == 2


def test_statistics_paginates_to_total_count(tmp_path, monkeypatch):
    def respond(path, body):
        offset = body["offset"]
        size = 1000 if offset == 0 else 1
        return post_raw(
            path,
            {
                "result": {
                    "dataTotalCount": 1001,
                    "groupings": [
                        {
                            "id": offset + number + 1,
                            "type": "totals",
                            "metrics": [{"slug": "views", "value": 1}],
                        }
                        for number in range(size)
                    ],
                }
            },
        )

    client = StatsClient(respond)
    exporter, _ = run_stats(tmp_path, monkeypatch, [item_window()], client)
    assert [body["offset"] for _, body in client.calls] == [0, 1000]
    assert exporter.stats.statistics_records == 1001


def test_resume_statistics_skips_committed_window(tmp_path, monkeypatch):
    windows = [item_window("2025-01-01"), item_window("2025-01-02")]

    class Interrupted(StatsClient):
        def post(self, path, *, json_body, attempts=5):
            if json_body["dateFrom"] == "2025-01-02":
                raise KeyboardInterrupt("checkpoint")
            return super().post(path, json_body=json_body, attempts=attempts)

    monkeypatch.setattr(StatisticsBackfill, "windows", lambda self: iter(windows))
    monkeypatch.setattr("avito_raw_export.statistics.time.sleep", lambda _: None)
    first = Exporter("client", "secret", options(tmp_path))
    first.client.close()
    first.client = Interrupted()
    with pytest.raises(KeyboardInterrupt):
        first.run()
    second_client = StatsClient()
    second, _ = run_stats(
        tmp_path,
        monkeypatch,
        windows,
        second_client,
        resume_from=first.store.root,
    )
    assert len(second_client.calls) == 1
    assert second_client.calls[0][1]["dateFrom"] == "2025-01-02"
    assert second.stats.statistics_periods == 2


def test_malformed_statistics_response_is_partial_and_stops_family(tmp_path, monkeypatch):
    client = StatsClient(lambda path, body: post_raw(path, {"result": {"groupings": []}}))
    _, result = run_stats(
        tmp_path,
        monkeypatch,
        [item_window("2025-01-01"), item_window("2025-01-02")],
        client,
    )
    manifest = json.loads((result / "manifest.json").read_bytes())
    assert manifest["status"] == "partial"
    assert len(client.calls) == 1
    assert list((result / "raw/requests").glob("*.json"))


def test_empty_statistics_is_valid(tmp_path, monkeypatch):
    client = StatsClient(
        lambda path, body: post_raw(
            path, {"result": {"dataTotalCount": 0, "groupings": []}}
        )
    )
    exporter, result = run_stats(tmp_path, monkeypatch, [item_window()], client)
    assert exporter.stats.statistics_periods == 1
    assert exporter.stats.statistics_records == 0
    assert json.loads((result / "manifest.json").read_bytes())["status"] == "completed"


def test_documented_horizons_and_windows():
    backfill = StatisticsBackfill(object(), today=date(2026, 9, 6))
    windows = list(backfill.windows())
    items = [w for w in windows if w.source == "items_daily"]
    spendings = [w for w in windows if w.source == "spendings_daily"]
    assert len(items) == 270
    assert items[0].date_from == date(2025, 12, 11)
    assert items[-1].date_to == date(2026, 9, 6)
    assert spendings[0].date_from == date(2025, 4, 15)
    assert spendings[-1].date_to == date(2026, 9, 6)


def test_statistics_checkpoint_is_atomic(tmp_path):
    store = ExportStore(tmp_path)
    journal = Journal(store)
    rows = [
        {"grouping_type": "day", "record_id": "1", "metric": "views", "value": 1},
        {"grouping_type": "day", "record_id": "1", "metric": "bad", "value": object()},
    ]
    with pytest.raises(TypeError):
        journal.statistics_commit("key", "items", "2025-01-01", "2025-01-01", rows)
    assert journal.statistics_counts() == (0, 0)
    journal.close()


def test_v02_archive_can_be_opened(tmp_path):
    store = ExportStore(tmp_path)
    manifest = json.loads((store.root / "manifest.json").read_bytes())
    manifest["format"] = "avito-raw-export-v2"
    (store.root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert ExportStore.open_existing(store.root).manifest["format"] == "avito-raw-export-v2"


@respx.mock
def test_statistics_429_retry_after(monkeypatch):
    delays = []
    monkeypatch.setattr("avito_raw_export.client.time.sleep", delays.append)
    respx.post("https://api.avito.ru/token").respond(200, json={"access_token": "fake"})
    route = respx.post("https://api.avito.ru/stats/v2/accounts/1/items").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(200, json={"result": {"dataTotalCount": 0, "groupings": []}}),
        ]
    )
    with AvitoClient("client", "secret") as client:
        client.post(
            "/stats/v2/accounts/1/items",
            json_body={"dateFrom": "2025-01-01"},
        )
    assert route.call_count == 2 and delays == [7]
    assert route.calls[0].request.headers["X-AgencyClientId"] == "1"


@respx.mock
def test_statistics_deterministic_4xx_is_not_retried():
    respx.post("https://api.avito.ru/token").respond(200, json={"access_token": "fake"})
    route = respx.post("https://api.avito.ru/stats/v2/accounts/1/items").respond(
        400, json={"error": {"message": "bad request"}}
    )
    with AvitoClient("client", "secret") as client:
        with pytest.raises(AvitoApiError):
            client.post("/stats/v2/accounts/1/items", json_body={"invalid": True})
    assert route.call_count == 1
