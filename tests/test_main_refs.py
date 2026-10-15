from src import __main__ as main_mod


def test_refresh_refs_alone_does_not_fetch(monkeypatch):
    called = []
    monkeypatch.setattr(main_mod, "refresh_grid_ref", lambda: called.append("sgm"))
    monkeypatch.setattr(main_mod, "refresh_mastr_ref", lambda: called.append("mastr"))
    monkeypatch.setattr(main_mod, "fetch_country", lambda *a, **k: (_ for _ in ()).throw(AssertionError("fetch")))
    assert main_mod.main(["--refresh-refs"]) == 0
    assert called == ["sgm", "mastr"]


def test_refresh_refs_then_rebuild(monkeypatch):
    called = []
    monkeypatch.setattr(main_mod, "refresh_grid_ref", lambda: called.append("sgm"))
    monkeypatch.setattr(main_mod, "refresh_mastr_ref", lambda: called.append("mastr"))

    def fake_rebuild(argv=None):
        raise AssertionError("should not reach fetch")

    monkeypatch.setattr(main_mod, "OUTAGES_JSON", type("P", (), {"is_file": lambda self: False})())
    code = main_mod.main(["--refresh-refs", "--rebuild-network"])
    assert code == 1
    assert called == ["sgm", "mastr"]
