from datetime import timedelta

from fastapi import APIRouter

from app.api.deps import FrontendAuthDep, SessionDep
from app.core.config import settings
from app.core.security import create_access_token
from app.crud import get_or_create_user_from_api_key
from app.models import Token, UserCreate

router = APIRouter()


@router.post("/session", response_model=Token)
def create_session(
    session: SessionDep,
    _: FrontendAuthDep,
    user_in: UserCreate,
) -> Token:
    user = get_or_create_user_from_api_key(session=session, user_create=user_in)
    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    return Token(
        access_token=create_access_token(user.id, expires_delta=access_token_expires),
        token_type="bearer",
    )
