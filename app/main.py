from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes_admin import router as admin_router
from app.api.routes_auth import router as auth_router
from app.api.routes_bugs import router as bugs_router
from app.api.routes_health import router as health_router
from app.core.config import settings
from app.core.database import Base, engine
from app.core.logging import get_logger, setup_logging
from app.services.config_validation import validate_runtime_config
from app.services.schema_bootstrap import run_local_schema_upgrades

setup_logging()
logger = get_logger("app.main")

Base.metadata.create_all(bind=engine)


run_local_schema_upgrades()

config_validation = validate_runtime_config()
if config_validation["status"] == "error":
    logger.error("Runtime config validation found errors: %s", config_validation["checks"])
elif config_validation["status"] == "degraded":
    logger.warning("Runtime config validation found warnings: %s", config_validation["checks"])
else:
    logger.info("Runtime config validation succeeded.")

app = FastAPI(title=settings.app_name)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router, prefix="/auth", tags=["auth"])
app.include_router(health_router, prefix="/health", tags=["health"])
app.include_router(bugs_router, prefix="/bugs", tags=["bugs"])
app.include_router(admin_router, prefix="/admin", tags=["admin"])


@app.middleware("http")
async def log_requests(request, call_next):
    import time

    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        logger.exception(
            "Unhandled request error method=%s path=%s duration_ms=%s",
            request.method,
            request.url.path,
            duration_ms,
        )
        raise

    duration_ms = round((time.perf_counter() - start) * 1000, 2)
    logger.info(
        "Request method=%s path=%s status=%s duration_ms=%s",
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
    )
    return response


@app.get("/")
def read_root() -> dict[str, str]:
    return {"message": f"{settings.app_name} API is running"}
