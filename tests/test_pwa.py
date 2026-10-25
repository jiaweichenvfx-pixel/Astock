"""PWA / 手机访问：清单与 Service Worker 可访问、访问口令生效。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app import main as app_main

STATIC = Path(config.STATIC_DIR)


# ── PWA 资源 ─────────────────────────────────────────────────────
def test_manifest_file_is_valid_json():
    data = json.loads((STATIC / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert data["display"] == "standalone"
    assert data["start_url"] == "/"
    assert data["theme_color"]
    sizes = {i["sizes"] for i in data["icons"]}
    assert "192x192" in sizes and "512x512" in sizes
    # 至少有一张 maskable，Android 才会用自适应图标
    assert any(i.get("purpose") == "maskable" for i in data["icons"])


def test_manifest_icons_exist():
    data = json.loads((STATIC / "manifest.webmanifest").read_text(encoding="utf-8"))
    for icon in data["icons"]:
        p = STATIC / Path(icon["src"]).name
        assert p.exists(), icon["src"]


def test_service_worker_file_has_cache_and_api_bypass():
    js = (STATIC / "sw.js").read_text(encoding="utf-8")
    assert "install" in js and "fetch" in js
    assert "/api/" in js          # 行情接口不缓存
    assert "caches" in js


def test_index_declares_manifest_and_sw():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'rel="manifest"' in html
    assert "/manifest.webmanifest" in html
    assert "serviceWorker" in html
    assert "theme-color" in html


# ── 路由 ─────────────────────────────────────────────────────────
@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(app_main, "ACCESS_TOKEN", "")
    return TestClient(app_main.app)


def test_manifest_route_content_type(client):
    r = client.get("/manifest.webmanifest")
    assert r.status_code == 200
    assert "manifest+json" in r.headers["content-type"]
    assert json.loads(r.text)["short_name"]


def test_sw_served_from_root(client):
    r = client.get("/sw.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]


def test_home_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "看长做短" in r.text


# ── 访问口令（可选）───────────────────────────────────────────────
def test_api_open_when_no_token(monkeypatch):
    monkeypatch.setattr(app_main, "ACCESS_TOKEN", "")
    c = TestClient(app_main.app)
    assert c.get("/api/ping").status_code == 200


def test_api_requires_token_when_configured(monkeypatch):
    monkeypatch.setattr(app_main, "ACCESS_TOKEN", "s3cret")
    c = TestClient(app_main.app)
    assert c.get("/api/ping").status_code == 401
    assert c.get("/api/ping", headers={"X-Auth-Token": "s3cret"}).status_code == 200
    assert c.get("/api/ping?token=s3cret").status_code == 200


def test_static_and_home_not_blocked_by_token(monkeypatch):
    monkeypatch.setattr(app_main, "ACCESS_TOKEN", "s3cret")
    c = TestClient(app_main.app)
    assert c.get("/").status_code == 200
    assert c.get("/manifest.webmanifest").status_code == 200


# ── 首页占位符必须有人填 ─────────────────────────────────────────
def test_footer_strategy_text_is_rendered():
    """曾经踩过：底部「今日策略：加载中…」没人填，永远是加载中。"""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "function renderStrategy" in html
    assert "renderStrategy();" in html
    assert "getElementById('strategyText')" in html


def test_no_orphan_loading_placeholders():
    """页面里每个「加载中」占位符都要有对应的写入点，否则会一直转圈。"""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for element_id in ("miSession", "miIndices", "miBreadth", "banner", "strategyText"):
        assert f'id="{element_id}"' in html
        assert f"'{element_id}'" in html or f'("{element_id}")' in html, element_id
