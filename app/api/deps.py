from collections.abc import Generator
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from jwt import InvalidTokenError
from sqlmodel import Session

from app.core.db import engine
from app.core.security import decode_access_token, verify_frontend_api_key
from app.models import TokenPayload, User

bearer_scheme = HTTPBearer()
frontend_api_key_scheme = APIKeyHeader(name="X-API-Key")


def get_db() -> Generator[Session]:
    with Session(engine) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_db)]
TokenDep = Annotated[HTTPAuthorizationCredentials, Depends(bearer_scheme)]
FrontendApiKeyDep = Annotated[str, Depends(frontend_api_key_scheme)]


def require_frontend_api_key(api_key: FrontendApiKeyDep) -> None:
    if not verify_frontend_api_key(api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )


FrontendAuthDep = Annotated[None, Depends(require_frontend_api_key)]


def get_current_user(session: SessionDep, credentials: TokenDep) -> User:
    try:
        payload = TokenPayload(**decode_access_token(credentials.credentials))
    except (InvalidTokenError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Could not validate credentials",
        ) from None

    user = session.get(User, payload.sub)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
