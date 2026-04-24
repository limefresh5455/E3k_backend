from fastapi import APIRouter, HTTPException

from app.config import ADMIN_PASSWORD, ADMIN_USERNAME
from app.schemas.auth import LoginRequest, LoginResponse
from app.services.auth_service import create_token

router = APIRouter()


@router.post("/api/login", response_model=LoginResponse)
def login(body: LoginRequest):
    if body.username != ADMIN_USERNAME or body.password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return LoginResponse(token=create_token(body.username), username=body.username)

