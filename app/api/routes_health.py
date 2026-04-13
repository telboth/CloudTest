from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.services.health import get_live_health, get_ready_health

router = APIRouter()


@router.get("/live")
def health_live() -> dict:
    return get_live_health()


@router.get("/ready")
def health_ready() -> JSONResponse:
    payload = get_ready_health()
    status_code = 200 if payload.get("status") in {"ok", "degraded"} else 503
    return JSONResponse(content=payload, status_code=status_code)
