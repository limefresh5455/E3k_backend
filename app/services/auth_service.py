from datetime import datetime, timedelta
from typing import Optional

import jwt
from fastapi import Header, HTTPException

from app.config import JWT_ALGO, JWT_EXPIRE_HOURS, JWT_SECRET


def create_token(username: str) -> str:
    payload = {"sub": username, "exp": datetime.utcnow() + timedelta(hours=JWT_EXPIRE_HOURS)}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)


def verify_token(authorization: Optional[str] = Header(None)) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid token")

    token = authorization.split(" ", 1)[1]
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
        return payload["sub"]
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

