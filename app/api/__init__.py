from fastapi import APIRouter

from .auth import router as auth_router
from .erp_sync import router as erp_sync_router
from .health import router as health_router
from .invoice import router as invoice_router
from .orders import router as orders_router
from .sync import router as sync_router

api_router = APIRouter()
api_router.include_router(auth_router)
api_router.include_router(sync_router)
api_router.include_router(erp_sync_router)
api_router.include_router(orders_router)
api_router.include_router(health_router)
api_router.include_router(invoice_router)
