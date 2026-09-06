from avito_raw_export import app


def test_entrypoint_binds_loopback_and_registers_per_client_page(monkeypatch):
    pages = []
    options = {}

    def page(path):
        def register(handler):
            pages.append((path, handler))
            return handler

        return register

    monkeypatch.setattr(app.ui, "page", page)
    monkeypatch.setattr(app.ui, "run", lambda **kwargs: options.update(kwargs))
    app.main()
    assert pages == [("/", app.build_page)]
    assert options["host"] == "127.0.0.1"
    assert options["port"] == 8765
    assert options["reload"] is False
