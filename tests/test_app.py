from pathlib import Path

from avito_raw_export import app


def test_entrypoint_binds_loopback_and_registers_per_client_page(monkeypatch):
    pages = []
    routes = []
    options = {}

    def page(path):
        def register(handler):
            pages.append((path, handler))
            return handler

        return register

    def get(path):
        def register(handler):
            routes.append((path, handler))
            return handler

        return register

    monkeypatch.setattr(app.ui, "page", page)
    monkeypatch.setattr(app.ui, "run", lambda **kwargs: options.update(kwargs))
    monkeypatch.setattr(app.nice_app, "get", get)
    app.main()
    assert pages == [("/", app.build_page)]
    assert routes == [("/_diagnostics/runtime", app.runtime_diagnostics)]
    assert options["host"] == "127.0.0.1"
    assert options["port"] == 8765
    assert options["reload"] is False


def test_runtime_diagnostics_identifies_loaded_client():
    diagnostics = app.runtime_diagnostics()
    assert diagnostics["build_marker"] == app.BUILD_MARKER
    assert diagnostics["module_version"] == "0.3.1"
    assert Path(diagnostics["package_file"]).parts[-2:] == (
        "avito_raw_export",
        "__init__.py",
    )
    assert Path(diagnostics["client_file"]).parts[-2:] == (
        "avito_raw_export",
        "client.py",
    )
    assert diagnostics["legacy_oauth_message_present"] is False
