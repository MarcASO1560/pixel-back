from datetime import timedelta

from fastapi import APIRouter, HTTPException, status

from app.api.deps import SessionDep
from app.core.config import settings
from app.core.security import create_access_token, verify_frontend_auth_token
from app.crud import get_or_create_user_from_api_key
from app.models import AuthSessionCreate, Token, UserCreate

router = APIRouter()


@router.post("/session", response_model=Token)
def create_session(
    session: SessionDep,
    session_in: AuthSessionCreate,
) -> Token:
    if not verify_frontend_auth_token(session_in.auth_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid auth token",
        )

    user_in = UserCreate.model_validate(session_in.model_dump(exclude={"auth_token"}))
    user = get_or_create_user_from_api_key(session=session, user_create=user_in)
    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    return Token(
        access_token=create_access_token(user.id, expires_delta=access_token_expires),
        token_type="bearer",
    )
