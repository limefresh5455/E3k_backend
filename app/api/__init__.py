from fastapi import APIRouter

from .auth import router as auth_router
from .erp_sync import router as erp_sync_router
from .finance_auth import router as finance_auth_router
from .health import router as health_router
from .invoice import router as invoice_router
from .orders import router as orders_router
from .sync import router as sync_router
from .suppliers import router as suppliers_router
from app.worker_dashboard.router import router as worker_dashboard_router
from app.worker_dashboard.s3_api import router as s3_router

api_router = APIRouter()
api_router.include_router(auth_router)
api_router.include_router(finance_auth_router)
api_router.include_router(sync_router)
api_router.include_router(erp_sync_router)
api_router.include_router(orders_router)
api_router.include_router(health_router)
api_router.include_router(invoice_router)
api_router.include_router(suppliers_router)
api_router.include_router(worker_dashboard_router)
api_router.include_router(s3_router)