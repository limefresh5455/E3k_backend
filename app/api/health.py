from fastapi import APIRouter

from app.config import API_VERSION

router = APIRouter()


@router.get("/health")
def health():
    return {"status": "ok", "version": API_VERSION}

