from fastapi import APIRouter, Depends, HTTPException

from app.services.auth_service import verify_token
from app.services.order_service import (
    get_order,
    get_order_by_number,
    get_stats,
    list_orders,
)

router = APIRouter()


@router.get("/api/orders")
def list_orders_endpoint(user: str = Depends(verify_token)):
    return list_orders()


@router.get("/api/orders/{order_id}")
def get_order_endpoint(order_id: int, user: str = Depends(verify_token)):
    order = get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


@router.get("/api/orders/by-number/{order_number}")
def get_order_by_number_endpoint(order_number: str, user: str = Depends(verify_token)):
    order = get_order_by_number(order_number)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


@router.get("/api/stats")
def get_stats_endpoint(user: str = Depends(verify_token)):
    return get_stats()

