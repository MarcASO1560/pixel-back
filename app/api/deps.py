from collections.abc import Generator
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import APIKeyHeader
from sqlmodel import Session

from app.core.db import engine
from app.core.security import verify_frontend_api_key
from app.crud import get_or_create_default_user
from app.models import User

frontend_api_key_scheme = APIKeyHeader(name="X-API-Key")


def get_db() -> Generator[Session]:
    with Session(engine) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_db)]
FrontendApiKeyDep = Annotated[str, Depends(frontend_api_key_scheme)]


def require_frontend_api_key(api_key: FrontendApiKeyDep) -> None:
    if not verify_frontend_api_key(api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )


FrontendAuthDep = Annotated[None, Depends(require_frontend_api_key)]


def get_current_user(session: SessionDep, _: FrontendAuthDep) -> User:
    return get_or_create_default_user(session=session)


CurrentUser = Annotated[User, Depends(get_current_user)]
