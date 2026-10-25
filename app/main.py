"""FastAPI 应用入口。"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import api, config, storage, warmup

# 可选访问口令：手机/局域网访问时设置 ASTOCK_TOKEN 即可，未设置则不校验
ACCESS_TOKEN = os.getenv("ASTOCK_TOKEN", "").strip()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 首次运行：写入演示账户（满仓融资被套场景），便于立刻看到效果
    if not storage.account_exists():
        storage.seed_demo_account()
    warmup.warmer.start()        # 后台预热，页面打开就有数据
    yield
    warmup.warmer.stop()


app = FastAPI(title="看长做短 V5.1 个人量化投研决策系统", lifespan=lifespan)


@app.middleware("http")
async def auth_guard(request, call_next):
    """设置 ASTOCK_TOKEN 后，/api/* 必须带口令（Header: X-Auth-Token 或 ?token=）。"""
    if ACCESS_TOKEN and request.url.path.startswith("/api/"):
        got = request.headers.get("x-auth-token") or request.query_params.get("token")
        if got != ACCESS_TOKEN:
            return JSONResponse({"detail": "未授权：请求头缺少 X-Auth-Token"}, status_code=401)
    return await call_next(request)


app.include_router(api.router)
app.mount("/static", StaticFiles(directory=str(config.STATIC_DIR)), name="static")


@app.get("/")
def index():
    return FileResponse(str(config.STATIC_DIR / "index.html"))


@app.get("/api/status")
def status():
    """缓存新鲜度 + 预热线程状态（界面用来显示「数据时间/更新中」）。"""
    return {
        "cache": api.cache_status(),
        "warmer": {
            "runs": warmup.warmer.runs,
            "last_error": warmup.warmer.last_error,
            "in_session": warmup.in_trading_session(),
        },
    }


@app.get("/manifest.webmanifest")
def manifest():
    """PWA 清单：必须从根路径提供，Chrome 才认为是可安装应用。"""
    return FileResponse(
        str(config.STATIC_DIR / "manifest.webmanifest"),
        media_type="application/manifest+json",
    )


@app.get("/sw.js")
def service_worker():
    """Service Worker 必须挂在根路径，scope 才能覆盖整个站点。"""
    return FileResponse(
        str(config.STATIC_DIR / "sw.js"),
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )
