from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .errors import FinanceAuthConfigurationError, InvalidTokenError
from .service import authenticated_user_from_access_token

_bearer = HTTPBearer(auto_error=False)


def require_finance_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict:
    if credentials is None or credentials.scheme.casefold() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Finanz-Cockpit token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return authenticated_user_from_access_token(credentials.credentials)
    except FinanceAuthConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired Finanz-Cockpit token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def require_finance_admin(user: dict = Depends(require_finance_user)) -> dict:
    if user["role"] != "finance_admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Finanz-Cockpit administrator access required",
        )
    return user
