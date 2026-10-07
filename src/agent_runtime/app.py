import logging
from contextlib import asynccontextmanager
from threading import Event, Thread

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException

from agent_runtime.api import completion_router, error_response, router
from agent_runtime.auth import Auth
from agent_runtime.backend import FakeBackend
from agent_runtime.clock import Clock
from agent_runtime.config import Settings
from agent_runtime.db import Database
from agent_runtime.executions import APIError
from agent_runtime.failpoints import Failpoints
from agent_runtime.kubernetes import KubernetesBackend
from agent_runtime.mockworkday import MockWorkday
from agent_runtime.reconciler import Reconciler
from agent_runtime.seed import seed


def create_app(
    *, db=None, mw=None, clock=None, backend=None, failpoints=None, reconcile=True, settings=None
):
    owns_db = db is None
    owns_mw = mw is None
    settings = settings or (Settings.from_env() if db is None else Settings(str(db.engine.url)))
    owns_backend = backend is None
    if db is None:
        db = Database(settings.database_url)
        db.initialize()
        seed(db, settings.mw_base_url)
    mw = mw or MockWorkday()
    clock = clock or Clock()
    if backend is None:
        if settings.workload_backend == "fake":
            backend = FakeBackend()
        elif settings.workload_backend == "kubernetes":
            backend = KubernetesBackend(settings)
        else:
            raise ValueError("Unknown workload backend")
    failpoints = failpoints or Failpoints()
    reconciler = Reconciler(db, backend, failpoints, settings.max_active_executions)
    stop = Event()

    def loop():
        while not stop.is_set():
            try:
                reconciler.reconcile_once(clock.now())
            except Exception:
                logging.getLogger(__name__).exception("Reconcile pass failed")
            stop.wait(1)

    @asynccontextmanager
    async def lifespan(app):
        thread = Thread(target=loop, daemon=True) if reconcile else None
        if thread:
            thread.start()
        try:
            yield
        finally:
            stop.set()
            if thread:
                thread.join()
            if owns_mw:
                mw.client.close()
            if owns_backend and isinstance(backend, KubernetesBackend):
                backend.client.close()
            if owns_db:
                db.engine.dispose()

    app = FastAPI(title="Agent Runtime R2", lifespan=lifespan)
    app.state.db, app.state.mw, app.state.clock = db, mw, clock
    app.state.backend, app.state.failpoints = backend, failpoints
    app.state.reconciler = reconciler
    app.state.auth = Auth(db, mw, clock)
    app.include_router(router)
    app.include_router(completion_router)
    install_error_handlers(app)
    return app


def create_completion_app(runtime):
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None, redirect_slashes=False)
    # Share the live state so a controller restart cannot leave cleanup on an old backend.
    app.state = runtime.state
    app.include_router(completion_router)
    install_error_handlers(app)
    return app


def install_error_handlers(app):

    @app.exception_handler(APIError)
    def api_error(request, exc):
        return error_response(request, exc.status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    def validation_error(request, exc):
        return error_response(request, 422, "INVALID_REQUEST", "Request does not match the schema")

    @app.exception_handler(SQLAlchemyError)
    def database_error(request, exc):
        return error_response(request, 503, "UNAVAILABLE", "Database unavailable")

    @app.exception_handler(HTTPException)
    def http_error(request, exc):
        return error_response(request, exc.status_code, "HTTP_ERROR", str(exc.detail))
