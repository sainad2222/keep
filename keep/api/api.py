import asyncio
import logging
import os
from importlib import metadata

import requests
import uvicorn
from dotenv import find_dotenv, load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware
from starlette_context import plugins
from starlette_context.middleware import RawContextMiddleware

import keep.api.logging
import keep.api.observability
import keep.api.utils.import_ee
from keep.api.arq_worker import get_arq_worker
from keep.api.consts import (
    KEEP_ARQ_QUEUE_BASIC,
    KEEP_ARQ_TASK_POOL,
    KEEP_ARQ_TASK_POOL_ALL,
    KEEP_ARQ_TASK_POOL_BASIC_PROCESSING,
    KEEP_ARQ_TASK_POOL_NONE,
)
from keep.api.core.dependencies import SINGLE_TENANT_UUID
from keep.api.logging import CONFIG as logging_config
from keep.api.middlewares import LoggingMiddleware
from keep.api.routes import (
    actions,
    ai,
    alerts,
    dashboard,
    deduplications,
    extraction,
    healthcheck,
    incidents,
    maintenance,
    mapping,
    metrics,
    preset,
    providers,
    pusher,
    rules,
    settings,
    status,
    tags,
    topology,
    whoami,
    workflows,
)
from keep.api.routes.auth import groups as auth_groups
from keep.api.routes.auth import permissions, roles, users
from keep.event_subscriber.event_subscriber import EventSubscriber
from keep.identitymanager.identitymanagerfactory import (
    IdentityManagerFactory,
    IdentityManagerTypes,
)

# load all providers into cache
from keep.providers.providers_factory import ProvidersFactory
from keep.workflowmanager.workflowmanager import WorkflowManager

load_dotenv(find_dotenv())
keep.api.logging.setup_logging()
logger = logging.getLogger(__name__)

HOST = os.environ.get("KEEP_HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", 8080))
SCHEDULER = os.environ.get("SCHEDULER", "true") == "true"
CONSUMER = os.environ.get("CONSUMER", "true") == "true"

AUTH_TYPE = os.environ.get("AUTH_TYPE", IdentityManagerTypes.NOAUTH.value).lower()
try:
    KEEP_VERSION = metadata.version("keep")
except Exception:
    KEEP_VERSION = os.environ.get("KEEP_VERSION", "unknown")

# Monkey patch requests to disable redirects
original_request = requests.Session.request


def no_redirect_request(self, method, url, **kwargs):
    kwargs["allow_redirects"] = False
    return original_request(self, method, url, **kwargs)


requests.Session.request = no_redirect_request


def get_app(
    auth_type: IdentityManagerTypes = IdentityManagerTypes.NOAUTH.value,
) -> FastAPI:
    if not os.environ.get("KEEP_API_URL", None):
        logger.info(
            "KEEP_API_URL is not set, setting it to default",
            extra={"keep_api_url": f"http://{HOST}:{PORT}"},
        )
        os.environ["KEEP_API_URL"] = f"http://{HOST}:{PORT}"

    logger.info(
        f"Starting Keep with {os.environ['KEEP_API_URL']} as URL and version {KEEP_VERSION}",
        extra={
            "keep_version": KEEP_VERSION,
            "keep_api_url": os.environ.get("KEEP_API_URL"),
        },
    )

    app = FastAPI(
        title="Keep API",
        description="Rest API powering https://platform.keephq.dev and friends 🏄‍♀️",
        version=KEEP_VERSION,
    )

    @app.get("/")
    async def root():
        """
        App description and version.
        """
        return {"message": app.description, "version": KEEP_VERSION}

    app.add_middleware(RawContextMiddleware, plugins=(plugins.RequestIdPlugin(),))
    app.add_middleware(
        GZipMiddleware, minimum_size=30 * 1024 * 1024
    )  # Approximately 30 MiB, https://cloud.google.com/run/quotas
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(providers.router, prefix="/providers", tags=["providers"])
    app.include_router(actions.router, prefix="/actions", tags=["actions"])
    app.include_router(ai.router, prefix="/ai", tags=["ai"])
    app.include_router(healthcheck.router, prefix="/healthcheck", tags=["healthcheck"])
    app.include_router(alerts.router, prefix="/alerts", tags=["alerts"])
    app.include_router(incidents.router, prefix="/incidents", tags=["incidents"])
    app.include_router(settings.router, prefix="/settings", tags=["settings"])
    app.include_router(
        workflows.router, prefix="/workflows", tags=["workflows", "alerts"]
    )
    app.include_router(whoami.router, prefix="/whoami", tags=["whoami"])
    app.include_router(pusher.router, prefix="/pusher", tags=["pusher"])
    app.include_router(status.router, prefix="/status", tags=["status"])
    app.include_router(rules.router, prefix="/rules", tags=["rules"])
    app.include_router(preset.router, prefix="/preset", tags=["preset"])
    app.include_router(
        mapping.router, prefix="/mapping", tags=["enrichment", "mapping"]
    )
    app.include_router(
        auth_groups.router, prefix="/auth/groups", tags=["auth", "groups"]
    )
    app.include_router(
        permissions.router, prefix="/auth/permissions", tags=["auth", "permissions"]
    )
    app.include_router(roles.router, prefix="/auth/roles", tags=["auth", "roles"])
    app.include_router(users.router, prefix="/auth/users", tags=["auth", "users"])
    app.include_router(metrics.router, prefix="/metrics", tags=["metrics"])
    app.include_router(
        extraction.router, prefix="/extraction", tags=["enrichment", "extraction"]
    )
    app.include_router(dashboard.router, prefix="/dashboard", tags=["dashboard"])
    app.include_router(tags.router, prefix="/tags", tags=["tags"])
    app.include_router(maintenance.router, prefix="/maintenance", tags=["maintenance"])
    app.include_router(topology.router, prefix="/topology", tags=["topology"])
    app.include_router(
        deduplications.router, prefix="/deduplications", tags=["deduplications"]
    )
    # if its single tenant with authentication, add signin endpoint
    logger.info(f"Starting Keep with authentication type: {AUTH_TYPE}")
    # If we run Keep with SINGLE_TENANT auth type, we want to add the signin endpoint
    identity_manager = IdentityManagerFactory.get_identity_manager(
        SINGLE_TENANT_UUID, None, AUTH_TYPE
    )
    # if any endpoints needed, add them on_start
    identity_manager.on_start(app)

    @app.on_event("startup")
    async def on_startup():
        logger.info("Loading providers into cache")
        ProvidersFactory.get_all_providers()
        # Start the services
        logger.info("Starting the services")
        # Start the scheduler
        if SCHEDULER:
            logger.info("Starting the scheduler")
            wf_manager = WorkflowManager.get_instance()
            await wf_manager.start()
            logger.info("Scheduler started successfully")
        # Start the consumer
        if CONSUMER:
            logger.info("Starting the consumer")
            event_subscriber = EventSubscriber.get_instance()
            # TODO: there is some "race condition" since if the consumer starts before the server,
            #       and start getting events, it will fail since the server is not ready yet
            #       we should add a "wait" here to make sure the server is ready
            await event_subscriber.start()
            logger.info("Consumer started successfully")
        if KEEP_ARQ_TASK_POOL != KEEP_ARQ_TASK_POOL_NONE:
            event_loop = asyncio.get_event_loop()
            if KEEP_ARQ_TASK_POOL == KEEP_ARQ_TASK_POOL_ALL:
                logger.info("Starting all task pools")
                basic_worker = get_arq_worker(KEEP_ARQ_QUEUE_BASIC)
                event_loop.create_task(basic_worker.async_run())
            elif KEEP_ARQ_TASK_POOL == KEEP_ARQ_TASK_POOL_BASIC_PROCESSING:
                logger.info("Starting Basic Processing task pool")
                arq_worker = get_arq_worker(KEEP_ARQ_QUEUE_BASIC)
                event_loop.create_task(arq_worker.async_run())
            else:
                raise ValueError(f"Invalid task pool: {KEEP_ARQ_TASK_POOL}")
        logger.info("Services started successfully")

    @app.on_event("shutdown")
    async def on_shutdown():
        logger.info("Shutting down Keep")
        if SCHEDULER:
            logger.info("Stopping the scheduler")
            wf_manager = WorkflowManager.get_instance()
            # stop the scheduler
            try:
                await wf_manager.stop()
            # in pytest, there could be race condition
            except TypeError:
                pass
            logger.info("Scheduler stopped successfully")
        if CONSUMER:
            logger.info("Stopping the consumer")
            event_subscriber = EventSubscriber.get_instance()
            try:
                await event_subscriber.stop()
            # in pytest, there could be race condition
            except TypeError:
                pass
            logger.info("Consumer stopped successfully")
        # ARQ workers stops themselves? see "shutdown on SIGTERM" in logs
        logger.info("Keep shutdown complete")

    @app.exception_handler(Exception)
    async def catch_exception(request: Request, exc: Exception):
        logging.error(
            f"An unhandled exception occurred: {exc}, Trace ID: {request.state.trace_id}. Tenant ID: {request.state.tenant_id}"
        )
        return JSONResponse(
            status_code=500,
            content={
                "message": "An internal server error occurred.",
                "trace_id": request.state.trace_id,
                "error_msg": str(exc),
            },
        )

    app.add_middleware(LoggingMiddleware)

    keep.api.observability.setup(app)

    return app


def run(app: FastAPI):
    logger.info("Starting the uvicorn server")
    # call on starting to create the db and tables
    import keep.api.config

    keep.api.config.on_starting()

    # run the server
    uvicorn.run(
        app,
        host=HOST,
        port=PORT,
        log_config=logging_config,
    )
