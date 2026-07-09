"""Widget static assets: the /widget-app mount, its legacy /chat alias, and the
loader route's enabled-key gate. The /chat alias exists because loaders cached
before the /widget-app path fix keep requesting /chat/index.html — both paths
must serve the SAME chat app or the widget panel renders the admin login page
(production incident)."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.app.db import get_session
from apps.api.app.main import _mount_widget_assets
from apps.api.app.modules.widget import service as widget_service


@pytest.fixture()
def dist(tmp_path: Path) -> Path:
    (tmp_path / "loader.js").write_text("// loader", encoding="utf-8")
    chat = tmp_path / "chat"
    chat.mkdir()
    (chat / "index.html").write_text("<!doctype html><title>chat</title>", encoding="utf-8")
    return tmp_path


def _client(dist: Path, monkeypatch, *, widget=None, raise_db=False) -> TestClient:
    app = FastAPI()

    async def fake_get_widget_by_key(session, key):
        if raise_db:
            raise RuntimeError("db down")
        return widget

    monkeypatch.setattr(widget_service, "get_widget_by_key", fake_get_widget_by_key)
    _mount_widget_assets(app, dist_dir=dist)

    async def fake_session():
        yield SimpleNamespace()

    app.dependency_overrides[get_session] = fake_session
    return TestClient(app)


def test_chat_alias_serves_same_body_as_widget_app(dist, monkeypatch):
    c = _client(dist, monkeypatch, widget=SimpleNamespace(id="w1"))
    a = c.get("/widget-app/index.html")
    b = c.get("/chat/index.html")
    assert a.status_code == 200 and b.status_code == 200
    assert a.content == b.content


def test_loader_served_with_cache_headers_for_enabled_key(dist, monkeypatch):
    c = _client(dist, monkeypatch, widget=SimpleNamespace(id="w1"))
    r = c.get("/js/project_cb7a196a5d9306f5.js")
    assert r.status_code == 200
    assert "max-age=3600" in r.headers["Cache-Control"]
    assert "javascript" in r.headers["content-type"]


def test_loader_404_no_store_for_unknown_or_disabled_key(dist, monkeypatch):
    c = _client(dist, monkeypatch, widget=None)  # get_widget_by_key filters enabled
    r = c.get("/js/project_6d0c44de280b1fc3.js")
    assert r.status_code == 404
    assert r.headers["Cache-Control"] == "no-store"


def test_loader_404_for_non_alnum_key(dist, monkeypatch):
    c = _client(dist, monkeypatch, widget=SimpleNamespace(id="w1"))
    assert c.get("/js/project_..%2Fetc.js").status_code == 404


def test_loader_fails_open_on_db_error(dist, monkeypatch):
    # a transient DB hiccup must never take every merchant's widget down
    c = _client(dist, monkeypatch, raise_db=True)
    assert c.get("/js/project_cb7a196a5d9306f5.js").status_code == 200
