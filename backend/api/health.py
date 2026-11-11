from fastapi import APIRouter

from backend.schemas.health import HealthResponse

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
def health():
    return HealthResponse(status="ok")
