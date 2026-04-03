from fastapi import APIRouter

from src.api.v1.endpoints.agents import router as agents_router
from src.api.v1.endpoints.sites import router as sites_router

router = APIRouter(prefix="/api/v1")

router.include_router(agents_router)
router.include_router(sites_router)
