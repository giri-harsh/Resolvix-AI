"""FastAPI application factory."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import yaml
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from resolvix_contracts import DepartmentsConfig

from .clock import Clock
from .logfilter import install_access_log_masking
from .timeutil import iso_utc
from .ratelimit import SlidingWindowLimiter
from .config import Settings, load_settings
from .db import create_db_engine, get_read_session_factory, get_session_factory
from .errors import validation_error_handler, http_exception_handler, general_exception_handler
from .orm import Base, Ticket, TicketSeq
from .routers import health, auth_routes, config_routes
from .seed_users import seed_users_if_needed
from .services.notify import Notifier, ShardedExecutor, SmsNotifier
from .sms.policy import load_sms_config, staff_alert_numbers
from .sms.providers import build_provider
from .sms.service import SmsService
from .sse import SSEHub


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup/shutdown."""
    # Startup: capture event loop for SSE hub
    app.state.sse_hub.set_loop(asyncio.get_running_loop())
    
    yield
    
    # Shutdown: stop publishing, dispose engine
    app.state.sse_hub.set_loop(None)
    app.state.sms_executor.shutdown(wait=True)  # let in-flight status SMS finish
    app.state.engine.dispose()


def create_app(settings: Settings | None = None, notifier: Notifier | None = None) -> FastAPI:
    """Create and configure FastAPI application.
    
    Args:
        settings: Settings object (None = load from env)
        notifier: Notifier implementation (None = SmsNotifier on the SMS service)
    """
    
    if settings is None:
        settings = load_settings()
    
    # SMS provider first: NOTIFY_MODE=twilio with missing credentials must fail before anything else starts
    provider = build_provider(settings)

    # Create database engine
    engine = create_db_engine(settings)
    session_factory = get_session_factory(engine)
    
    # Create tables
    Base.metadata.create_all(bind=engine)
    
    # Initialize ticket sequence if needed
    with session_factory() as db:
        seq = db.query(TicketSeq).first()
        if seq is None:
            seq = TicketSeq(id=1, next_num=1001)
            db.add(seq)
            db.commit()
    
    # Load and validate departments config
    departments_path = settings.resolve_path(settings.departments_file)
    with open(departments_path, "r", encoding="utf-8") as f:
        departments_raw = yaml.safe_load(f)
    departments_config = DepartmentsConfig.model_validate(departments_raw)
    
    # Seed users if enabled
    if settings.seed_users_on_startup:
        with session_factory() as db:
            departments_keys = {d.key for d in departments_config.departments}
            seed_users_if_needed(db, settings, departments_keys)
    
    # SMS service and the default (real) notifier built on it
    sms_service = SmsService(
        settings, session_factory, load_sms_config(settings.resolve_path(settings.sms_templates_file)),
        staff_alert_numbers(departments_config), provider)
    sms_executor = ShardedExecutor(2, "sms")  # threads start on first submit; FIFO per ticket
    if notifier is None:
        notifier = SmsNotifier(sms_service, sms_executor if settings.sms_async else None)

    # Create app
    # Interactive docs only on a local deployment: they are unauthenticated
    docs_on = settings.ops_public_url.lower().startswith(("http://localhost", "http://127.0.0.1", "http://[::1]"))
    app = FastAPI(
        title="Resolvix Ops API",
        version="1.0.0",
        description="Member C: Ops API, SMS service, Staff Dashboard backend",
        lifespan=lifespan,
        docs_url="/docs" if docs_on else None,
        redoc_url="/redoc" if docs_on else None,
        openapi_url="/openapi.json" if docs_on else None,
    )
    install_access_log_masking()
    
    # Store state for dependencies
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.read_session_factory = get_read_session_factory(engine)
    app.state.departments_config = departments_config
    app.state.notifier = notifier
    app.state.sms = sms_service
    app.state.clock = Clock(settings.timezone)
    app.state.login_limiter = SlidingWindowLimiter(settings.login_rate_limit_per_min)  # failures per (ip, email)
    app.state.login_ip_limiter = SlidingWindowLimiter(settings.login_rate_limit_per_min * 6)  # all attempts per ip
    app.state.sms_executor = sms_executor

    def publish_ticket_updated(ticket_id: str) -> None:
        """A background SMS finished: tell the dashboards so the ticket timeline refreshes."""
        with session_factory() as db:
            t = db.query(Ticket).filter(Ticket.ticket_id == ticket_id).first()
            if t is not None:
                app.state.sse_hub.publish("ticket.updated", {
                    "ticket_id": t.ticket_id, "status": t.status, "assignee": t.assignee, "priority": t.priority,
                    "department": t.department, "updated_at": iso_utc(t.updated_at)})

    sms_service.on_outcome = publish_ticket_updated
    app.state.sse_hub = SSEHub()
    
    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    
    # Error handlers
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    from fastapi import HTTPException
    from starlette.exceptions import HTTPException as StarletteHTTPException
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(Exception, general_exception_handler)
    
    # Include routers
    app.include_router(health.router)
    app.include_router(auth_routes.router)
    app.include_router(config_routes.router)
    
    # Import and include calls router
    from .routers import calls_routes, tickets_routes, sse_routes
    app.include_router(calls_routes.router)
    app.include_router(tickets_routes.router)
    app.include_router(sse_routes.router)
    from .routers import sms_routes, gaps_routes, kb_routes
    app.include_router(sms_routes.router)
    app.include_router(gaps_routes.router)
    app.include_router(kb_routes.router)
    
    return app


def __getattr__(name: str):
    """Lazy module-level `app` for `uvicorn resolvix_ops.app:app`.

    Built on first access so that importing this module (tests, tooling) never opens
    the default database.
    """
    if name == "app":
        global _app
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(name)


_app: FastAPI | None = None
