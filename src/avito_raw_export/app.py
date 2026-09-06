from __future__ import annotations

import asyncio
import importlib.metadata
import json
import os
import queue
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from nicegui import ui, app as nice_app
from platformdirs import user_documents_dir

import avito_raw_export
from . import __version__
from .client import AvitoClient
from . import client as client_module
from .recovery import unfinished_exports
from .store import atomic_json
from .config import Profile, ProfileStore
from .exporter import Exporter, ExportOptions, ExportStats

BUILD_MARKER = "Avito Raw Export v0.3.1 — build b4e51b2"


class AppState:
    def __init__(self) -> None:
        self.profile_store = ProfileStore()
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.running = False
        self.active_exporter = None
        self.last_export: Path | None = None
        self.task: asyncio.Task | None = None


def default_export_root() -> Path:
    preferences = ProfileStore().root / "ui.json"
    try:
        saved = json.loads(preferences.read_bytes()).get("export_root")
        if saved:
            return Path(saved)
    except (OSError, ValueError):
        pass
    return Path(user_documents_dir()) / "AvitoRawExport" / "exports"


def open_folder(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def build_page() -> None:
    state = AppState()
    ui.page_title("Avito Raw Export")

    with ui.header().classes("items-center justify-between"):
        ui.label("Avito Raw Export").classes("text-xl font-bold")
        with ui.column().classes("items-end gap-0"):
            ui.label("read-only • raw archive").classes("text-sm opacity-70")
            ui.label(BUILD_MARKER).classes("text-xs font-mono text-yellow-200")

    with ui.column().classes("w-full max-w-5xl mx-auto p-4 gap-4"):
        ui.label(BUILD_MARKER).classes(
            "w-full rounded bg-yellow-100 text-yellow-900 px-3 py-2 font-mono"
        )
        ui.label("Максимальная сырая выгрузка данных Avito API").classes(
            "text-2xl font-bold"
        )
        ui.label(
            "Инструмент ничего не отправляет и не меняет в Авито. Он использует только методы чтения и сохраняет ответы API как есть."
        ).classes("text-base opacity-80")

        with ui.card().classes("w-full"):
            ui.label("1. Аккаунт").classes("text-lg font-bold")
            with ui.row().classes("w-full items-end gap-3"):
                profile_select = ui.select(
                    options=[p.name for p in state.profile_store.list()],
                    label="Сохранённый профиль",
                ).classes("min-w-64")
                profile_name = ui.input(
                    "Название профиля", placeholder="Например: Бассейны"
                ).classes("min-w-64")
                client_id = ui.input("Client ID").classes("min-w-80")
                client_secret = ui.input(
                    "Client Secret", password=True, password_toggle_button=True
                ).classes("min-w-80")

            account_status = ui.label("Аккаунт не проверен").classes("text-sm")

            def load_profile() -> None:
                name = profile_select.value
                if not name:
                    return
                profile = next(
                    (p for p in state.profile_store.list() if p.name == name), None
                )
                if not profile:
                    return
                profile_name.value = profile.name
                client_id.value = profile.client_id
                client_secret.value = ""
                try:
                    client_secret.value = state.profile_store.secret(profile.name) or ""
                except Exception:
                    ui.notify(
                        "Системное хранилище секретов недоступно", type="negative"
                    )
                    return
                account_status.text = f"Профиль «{profile.name}» загружен"

            profile_select.on_value_change(lambda _: load_profile())

            async def check_connection() -> None:
                if not client_id.value or not client_secret.value:
                    ui.notify("Введите Client ID и Client Secret", type="warning")
                    return
                account_status.text = "Проверяю подключение…"
                credentials = (client_id.value, client_secret.value)

                def work() -> dict[str, Any]:
                    with AvitoClient(*credentials) as client:
                        client.authenticate()
                        return client.get("/core/v1/accounts/self").json()

                try:
                    payload = await asyncio.to_thread(work)
                    account_id = (
                        payload.get("id")
                        or payload.get("user_id")
                        or payload.get("userId")
                    )
                    account_status.text = f"✓ Подключение успешно. Account ID: {account_id or 'получен, ID не распознан'}"
                    ui.notify("Подключение к Avito API работает", type="positive")
                except Exception as exc:
                    account_status.text = f"Ошибка подключения: {exc}"
                    ui.notify(str(exc), type="negative", timeout=10000)

            def save_profile() -> None:
                name = (profile_name.value or "").strip()
                if not name or not client_id.value or not client_secret.value:
                    ui.notify(
                        "Заполните название профиля, Client ID и Client Secret",
                        type="warning",
                    )
                    return
                try:
                    state.profile_store.save(
                        Profile(name=name, client_id=client_id.value.strip()),
                        client_secret.value,
                    )
                    profile_select.options = [
                        p.name for p in state.profile_store.list()
                    ]
                    profile_select.update()
                    profile_select.value = name
                    ui.notify("Профиль сохранён локально", type="positive")
                except Exception as exc:
                    ui.notify(f"Не удалось сохранить профиль: {exc}", type="negative")

            with ui.row().classes("gap-2"):
                ui.button("Проверить подключение", on_click=check_connection)
                ui.button("Сохранить профиль", on_click=save_profile).props("outline")

        with ui.card().classes("w-full"):
            ui.label("2. Выгрузка").classes("text-lg font-bold")
            export_root = ui.input(
                "Папка для выгрузок", value=str(default_export_root())
            ).classes("w-full")
            resume_select = ui.select(
                options={}, label="Незавершённая выгрузка"
            ).classes("w-full")
            recovery_label = ui.label("").classes("text-sm")

            def refresh_exports():
                found = unfinished_exports(Path(export_root.value or ".").expanduser())
                resume_select.options = {
                    str(path): f"{path.name} — {manifest.get('status')}"
                    for path, manifest in found
                }
                resume_select.value = str(found[0][0]) if found else None
                resume_select.update()
                recovery_label.text = (
                    "Найдена незавершённая выгрузка. Можно продолжить её или начать новую."
                    if found
                    else "Незавершённых выгрузок в этой папке нет."
                )

            export_root.on_value_change(lambda _: refresh_exports())
            refresh_exports()
            ui.label(
                "По умолчанию забираем всю историю, которую реально отдаёт API. Период заранее не режем."
            ).classes("text-sm opacity-70")

            with ui.row().classes("gap-6"):
                opt_chats = ui.checkbox("Чаты и сообщения", value=True)
                opt_reviews = ui.checkbox("Отзывы", value=True)
                opt_statistics = ui.checkbox("Статистика", value=True)
            with ui.row().classes("gap-6"):
                download_media = ui.checkbox(
                    "Скачивать изображения и медиа", value=False
                )
                download_voice = ui.checkbox("Скачивать голосовые", value=False)

        with ui.card().classes("w-full"):
            ui.label("3. Процесс").classes("text-lg font-bold")
            stage_label = ui.label("Готов к запуску").classes("font-medium")
            detail_label = ui.label("")
            progress = ui.linear_progress(value=0).classes("w-full")

            with ui.row().classes("gap-8"):
                item_counter = ui.label("Объявления: 0")
                chat_counter = ui.label("Чаты: 0")
                message_counter = ui.label("Сообщения: 0")
                review_counter = ui.label("Отзывы: 0")
                statistics_counter = ui.label("Статистика: 0 записей / 0 периодов")
                media_counter = ui.label("Медиа: 0")
                error_counter = ui.label("Ошибки: 0")
                warning_counter = ui.label("Предупреждения: 0")
                failed_media_counter = ui.label("Не скачано медиа: 0")

            log_box = ui.log(max_lines=500).classes("w-full h-64")

            def emit_progress(stats: ExportStats) -> None:
                state.events.put(("progress", asdict(stats)))

            def emit_log(line: str) -> None:
                state.events.put(("log", line))

            async def start_export(resume=False) -> None:
                if state.running:
                    ui.notify("Выгрузка уже идёт", type="warning")
                    return
                if not client_id.value or not client_secret.value:
                    ui.notify("Сначала подключите аккаунт", type="warning")
                    return
                try:
                    if not (export_root.value or "").strip():
                        raise ValueError("Укажите папку выгрузок")
                    root = Path(export_root.value).expanduser()
                    root.mkdir(parents=True, exist_ok=True)
                except (OSError, ValueError) as exc:
                    ui.notify(f"Недоступна папка выгрузок: {exc}", type="negative")
                    return
                selected = (
                    Path(resume_select.value)
                    if resume and resume_select.value
                    else None
                )
                if resume and selected is None:
                    ui.notify("Выберите незавершённую выгрузку", type="warning")
                    return
                atomic_json(
                    state.profile_store.root / "ui.json", {"export_root": str(root)}
                )
                options = ExportOptions(
                    export_root=root,
                    resume_from=selected,
                    download_voice=bool(download_voice.value),
                    download_avito_media=bool(download_media.value),
                    statistics=bool(opt_statistics.value),
                    list_items=True,
                    item_details=True,
                    global_chats=bool(opt_chats.value),
                    chats_by_item=bool(opt_chats.value),
                    chat_details=bool(opt_chats.value),
                    messages=bool(opt_chats.value),
                    ratings_and_reviews=bool(opt_reviews.value),
                )
                state.running = True
                start_button.disable()
                resume_button.disable()
                stage_label.text = "Запуск…"
                progress.value = 0.02
                log_box.clear()

                credentials = (client_id.value, client_secret.value)

                def work() -> Path:
                    exporter = Exporter(
                        *credentials,
                        options,
                        on_progress=emit_progress,
                        on_log=emit_log,
                    )
                    state.active_exporter = exporter
                    return exporter.run()

                async def runner() -> None:
                    try:
                        result = await asyncio.to_thread(work)
                        state.events.put(("done", str(result)))
                    except BaseException as exc:
                        state.events.put(
                            (
                                "interrupted"
                                if isinstance(
                                    exc, (KeyboardInterrupt, asyncio.CancelledError)
                                )
                                else "fatal",
                                str(exc),
                            )
                        )

                state.task = asyncio.create_task(runner())

            start_button = ui.button(
                "Начать новую", on_click=lambda: start_export(False)
            ).classes("text-lg")
            resume_button = ui.button(
                "Продолжить последнюю выгрузку", on_click=lambda: start_export(True)
            ).classes("text-lg")

            def stop_export():
                if state.active_exporter:
                    state.active_exporter.request_stop()
                    recovery_label.text = (
                        "Остановка после текущего запроса; checkpoint будет сохранён."
                    )

            ui.button("Остановить с сохранением", on_click=stop_export).props("outline")
            nice_app.on_shutdown(stop_export)
            ui.button(
                "Открыть последнюю папку",
                on_click=lambda: open_folder(
                    state.last_export or Path(export_root.value)
                ),
            ).props("outline")

            stage_progress = {
                "connection": 0.03,
                "items": 0.10,
                "item_details": 0.20,
                "statistics_discovery": 0.25,
                "statistics_backfill": 0.38,
                "statistics_done": 0.52,
                "chats_global": 0.55,
                "chats_by_item": 0.64,
                "ratings": 0.70,
                "chat_details": 0.75,
                "messages": 0.82,
                "voice": 0.90,
                "media": 0.94,
                "done": 1.0,
            }

            def drain_events() -> None:
                while True:
                    try:
                        kind, payload = state.events.get_nowait()
                    except queue.Empty:
                        break
                    if kind == "log":
                        log_box.push(str(payload))
                    elif kind == "progress":
                        data = payload
                        stage = data.get("stage") or ""
                        stage_label.text = _stage_name(stage)
                        detail_label.text = data.get("detail") or ""
                        progress.value = stage_progress.get(stage, progress.value)
                        item_counter.text = f"Объявления: {data.get('items', 0)}"
                        chat_counter.text = f"Чаты: {data.get('chats', 0)}"
                        message_counter.text = (
                            f"Сообщения: {data.get('messages_seen', 0)}"
                        )
                        review_counter.text = f"Отзывы: {data.get('reviews_seen', 0)}"
                        statistics_counter.text = (
                            f"Статистика: {data.get('statistics_records', 0)} записей / "
                            f"{data.get('statistics_periods', 0)} периодов"
                        )
                        media_counter.text = f"Медиа: {data.get('media_files', 0)}"
                        error_counter.text = f"Ошибки: {data.get('errors', 0)}"
                        warning_counter.text = (
                            f"Предупреждения: {data.get('warnings', 0)}"
                        )
                        failed_media_counter.text = (
                            f"Не скачано медиа: {data.get('failed_media', 0)}"
                        )
                    elif kind == "done":
                        state.running = False
                        state.last_export = Path(payload)
                        start_button.enable()
                        resume_button.enable()
                        refresh_exports()
                        stage_label.text = "completed — выгрузка завершена"
                        detail_label.text = str(payload)
                        progress.value = 1.0

                        manifest = json.loads(
                            (state.last_export / "manifest.json").read_text(
                                encoding="utf-8"
                            )
                        )
                        recovery_label.text = (
                            "Экспорт восстановлен после остановки."
                            if manifest.get("recovered_at")
                            else ""
                        )
                        if manifest.get("status") == "partial":
                            stage_label.text = (
                                "partial — завершено с ошибками / ограничениями"
                            )
                            ui.notify(stage_label.text, type="warning")
                        else:
                            ui.notify("Выгрузка завершена", type="positive")
                    elif kind in ("fatal", "interrupted"):
                        state.running = False
                        start_button.enable()
                        resume_button.enable()
                        refresh_exports()
                        stage_label.text = (
                            "interrupted — можно продолжить"
                            if kind == "interrupted"
                            else "failed — можно продолжить"
                        )
                        detail_label.text = str(payload)
                        log_box.push(f"✗ {payload}")
                        ui.notify(str(payload), type="negative", timeout=15000)

            ui.timer(0.25, drain_events)

        with ui.card().classes("w-full"):
            ui.label("Что сохраняется").classes("text-lg font-bold")
            ui.markdown(
                """
- исходные ответы API в `raw/` без преобразования;
- метаданные каждого сохранённого ответа (`*.meta.json`);
- найденные ID объявлений, чатов и отзывов в `index/`;
- рейтинг и все доступные страницы опубликованных отзывов в `raw/ratings/`;
- дневная статистика объявлений, аккаунта и расходов в RAW и SQLite;
- ссылки и метаданные медиа остаются в RAW без изменений;
- изображения, медиа и голосовые скачиваются только при включении опций;
- `manifest.json` со счётчиками и границами истории;
- ошибки и журнал запросов без токенов/секретов.

**Важно:** папки выгрузки, профили и секреты исключены из Git по умолчанию.
"""
            )


def main() -> None:
    ui.page("/")(build_page)
    nice_app.get("/_diagnostics/runtime")(runtime_diagnostics)
    ui.run(
        title="Avito Raw Export", host="127.0.0.1", reload=False, show=True, port=8765
    )


def runtime_diagnostics() -> dict[str, Any]:
    try:
        package_version = importlib.metadata.version("avito-raw-export")
    except importlib.metadata.PackageNotFoundError:
        package_version = None
    try:
        client_source = Path(client_module.__file__ or "").read_text(encoding="utf-8")
    except OSError:
        client_source = ""
    return {
        "build_marker": BUILD_MARKER,
        "module_version": __version__,
        "package_version": package_version,
        "executable": sys.executable,
        "cwd": os.getcwd(),
        "package_file": avito_raw_export.__file__,
        "client_file": client_module.__file__,
        "legacy_oauth_message_present": (
            ("OAuth network error " + "after retries") in client_source
        ),
        "sys_path": sys.path,
    }


def _stage_name(stage: str) -> str:
    return {
        "connection": "Подключение к Avito",
        "items": "Получение объявлений",
        "item_details": "Карточки объявлений",
        "statistics_discovery": "Подготовка исторической статистики",
        "statistics_backfill": "Историческая статистика",
        "statistics_done": "Статистика сохранена",
        "chats_global": "Глобальный поиск чатов",
        "chats_by_item": "Поиск чатов через объявления",
        "ratings": "Рейтинг и отзывы",
        "chat_details": "Получение объектов чатов",
        "messages": "Выгрузка сообщений",
        "voice": "Голосовые сообщения",
        "media": "Медиафайлы",
        "done": "Готово",
    }.get(stage, stage or "Работа")


if __name__ == "__main__":
    main()
