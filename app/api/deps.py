from collections.abc import Generator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import InvalidTokenError
from sqlmodel import Session

from app.core.db import engine
from app.core.security import decode_access_token
from app.models import TokenPayload, User

bearer_scheme = HTTPBearer()
ACCESS_TOKEN_COOKIE_NAME = "sefkira_access_token"


def get_db() -> Generator[Session]:
    with Session(engine) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_db)]
TokenDep = Annotated[HTTPAuthorizationCredentials, Depends(bearer_scheme)]


def get_user_from_access_token(*, session: Session, access_token: str) -> User:
    try:
        payload = TokenPayload(**decode_access_token(access_token))
    except (InvalidTokenError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Could not validate credentials",
        ) from None

    user = session.get(User, payload.sub)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return user


def get_current_user(session: SessionDep, credentials: TokenDep) -> User:
    return get_user_from_access_token(session=session, access_token=credentials.credentials)


def get_current_user_from_cookie(session: SessionDep, request: Request) -> User:
    access_token = request.cookies.get(ACCESS_TOKEN_COOKIE_NAME)
    if not access_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Could not validate credentials",
        )

    return get_user_from_access_token(session=session, access_token=access_token)


CurrentUser = Annotated[User, Depends(get_current_user)]
CookieCurrentUser = Annotated[User, Depends(get_current_user_from_cookie)]
