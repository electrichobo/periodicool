"""FastAPI application factory."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import Depends, FastAPI

from periodicool import __version__, auth, db
from periodicool.config import Settings, load_settings, with_saved_server_settings
from periodicool.library import get_setting, refresh_file_info
from periodicool.logs import LogSetup, stored_secrets

# Always allowed, so a wrong allowed-hosts list never locks out the machine itself.
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1", "[::1]")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = with_saved_server_settings(settings or load_settings())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings.config_dir.mkdir(parents=True, exist_ok=True)
        db.upgrade(settings.db_url)
        app.state.secret = auth.load_secret(settings.config_dir)
        auth.api_key(settings.config_dir)  # created on first start
        app.state.engine = db.make_engine(settings.db_url)
        app.state.sessions = db.make_session_factory(app.state.engine)
        with app.state.sessions() as session:
            level = get_setting(session, "log.level", "info")
            if settings.library_dir.is_dir():
                refresh_file_info(session, settings.library_dir)
            session.commit()
        with app.state.sessions() as session:
            from periodicool.preferences import apply_folders

            apply_folders(session)
        app.state.logs = LogSetup(settings.config_dir, app.state.sessions, level)
        with app.state.sessions() as session:
            from periodicool.preferences import apply_display

            apply_display(session)
        with app.state.sessions() as session:
            app.state.logs.redactor.learn(stored_secrets(session))
        from periodicool.web.system import apply_verbose

        apply_verbose(app)
        from periodicool import notify

        app.state.notify = notify.start(app.state.sessions)
        from periodicool.web.api import start_commands

        start_commands(app)
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
            notify.stop()
            app.state.api_commands.stop()
            app.state.logs.detach()
            app.state.engine.dispose()

    from periodicool.web.security import auth_middleware, csrf_guard

    app = FastAPI(
        title="PeriodiCool",
        version=__version__,
        lifespan=lifespan,
        dependencies=[Depends(csrf_guard)],
        # Swagger UI and ReDoc load scripts from a CDN; the schema is served as JSON
        # at /api/v1/openapi.json instead (API key needed).
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.started_at = datetime.now(UTC)
    from periodicool import web

    web.DISPLAY_TZ["name"] = settings.tz
    app.state.login_throttle = auth.LoginThrottle()
    app.middleware("http")(auth_middleware)

    from fastapi.staticfiles import StaticFiles

    from periodicool.web import STATIC_DIR
    from periodicool.web import router as web_router
    from periodicool.web.activity import router as activity_router
    from periodicool.web.api import ApiError, api_error
    from periodicool.web.api import router as api_router
    from periodicool.web.connect import router as connect_router
    from periodicool.web.filesystem import router as filesystem_router
    from periodicool.web.magazines import router as magazines_router
    from periodicool.web.metadata import router as metadata_router
    from periodicool.web.security import router as security_router
    from periodicool.web.settings import router as settings_router
    from periodicool.web.sources import router as sources_router
    from periodicool.web.system import router as system_router

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(security_router)
    app.include_router(web_router)
    app.include_router(magazines_router)
    app.include_router(metadata_router)
    app.include_router(activity_router)
    app.include_router(sources_router)
    app.include_router(connect_router)
    app.include_router(api_router)
    app.add_exception_handler(ApiError, api_error)
    app.include_router(filesystem_router)
    app.include_router(settings_router)
    app.include_router(system_router)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    if settings.allowed_hosts:
        from starlette.middleware.trustedhost import TrustedHostMiddleware

        app.add_middleware(
            TrustedHostMiddleware, allowed_hosts=[*settings.allowed_hosts, *LOCAL_HOSTS]
        )
    if settings.url_base:
        from periodicool.web.urlbase import UrlBase

        return UrlBase(app, settings.url_base)
    return app
