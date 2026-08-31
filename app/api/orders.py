from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.services.auth_service import verify_token
from app.services.order_service import (
    OrderPdfNotAvailableError,
    get_order,
    get_order_by_number,
    get_order_pdf_view,
    get_stats,
    list_orders,
)
from app.services.pcloud_service import PCloudConfigurationError, PCloudViewLinkError

router = APIRouter()


class OrderPdfViewResponse(BaseModel):
    order_id: int
    file_name: str
    view_url: str


@router.get("/api/orders")
def list_orders_endpoint(user: str = Depends(verify_token)):
    return list_orders()


@router.get("/api/orders/{order_id}")
def get_order_endpoint(order_id: int, user: str = Depends(verify_token)):
    order = get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


@router.get(
    "/api/orders/{order_id}/pdf-view-url",
    response_model=OrderPdfViewResponse,
)
def get_order_pdf_view_endpoint(order_id: int, user: str = Depends(verify_token)):
    try:
        pdf_view = get_order_pdf_view(order_id)
    except OrderPdfNotAvailableError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except PCloudConfigurationError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except PCloudViewLinkError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error

    if not pdf_view:
        raise HTTPException(status_code=404, detail="Order not found")
    return pdf_view


@router.get("/api/orders/by-number/{order_number}")
def get_order_by_number_endpoint(order_number: str, user: str = Depends(verify_token)):
    order = get_order_by_number(order_number)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


@router.get("/api/stats")
def get_stats_endpoint(user: str = Depends(verify_token)):
    return get_stats()

