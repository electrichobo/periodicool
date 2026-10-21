"""FastAPI application factory."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from periodicool import __version__, auth, db
from periodicool.config import Settings, load_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings.config_dir.mkdir(parents=True, exist_ok=True)
        db.upgrade(settings.db_url)
        app.state.secret = auth.load_secret(settings.config_dir)
        auth.api_key(settings.config_dir)  # created on first start
        app.state.engine = db.make_engine(settings.db_url)
        app.state.sessions = db.make_session_factory(app.state.engine)
        stop, task = asyncio.Event(), None
        if settings.jobs_enabled:
            from periodicool.jobs import Scheduler

            task = asyncio.create_task(Scheduler(app.state.sessions, settings).loop(stop))
        try:
            yield
        finally:
            stop.set()
            if task is not None:
                await task
            app.state.engine.dispose()

    from periodicool.web.security import auth_middleware, csrf_guard

    app = FastAPI(
        title="PeriodiCool",
        version=__version__,
        lifespan=lifespan,
        dependencies=[Depends(csrf_guard)],
    )
    app.state.settings = settings
    app.state.login_throttle = auth.LoginThrottle()
    app.middleware("http")(auth_middleware)

    from fastapi.staticfiles import StaticFiles

    from periodicool.web import STATIC_DIR
    from periodicool.web import router as web_router
    from periodicool.web.security import router as security_router

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(security_router)
    app.include_router(web_router)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    return app
