"""FastAPI 应用工厂：API、SSE 与 PWA 静态资源。"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ..core.container import AppContainer
from .routes import api_router


def create_app(container: AppContainer | None = None, start_scheduler: bool = True) -> FastAPI:
    app_container = container or AppContainer.build()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_scheduler:
            app_container.start_background()
        yield
        app_container.scheduler.stop()
        app_container.db.close()

    app = FastAPI(
        title="Better Buyer",
        description="优惠券采集、盯盘预测与半自动购买",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.container = app_container

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(api_router)

    web_dir = app_container.settings.web_dir

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest():
        return FileResponse(
            web_dir / "manifest.webmanifest",
            media_type="application/manifest+json",
        )

    @app.get("/sw.js", include_in_schema=False)
    def service_worker():
        return FileResponse(
            web_dir / "sw.js",
            media_type="application/javascript",
            headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
        )

    if web_dir.exists():
        app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web")

    return app


app = None


def get_app() -> FastAPI:
    """uvicorn 导入字符串入口：better_buyer.api.app:get_app --factory"""
    global app
    if app is None:
        app = create_app()
    return app
