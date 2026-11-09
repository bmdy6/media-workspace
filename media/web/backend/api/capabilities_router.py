"""Public, non-secret model and capability catalog."""
from fastapi import APIRouter

from media.core.runtime_config import public_capabilities

router = APIRouter(tags=["capabilities"])


@router.get("/api/capabilities")
def get_capabilities():
    return public_capabilities()
