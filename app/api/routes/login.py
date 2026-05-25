from fastapi import APIRouter

from app.api.deps import FrontendAuthDep, SessionDep
from app.crud import get_or_create_user_from_api_key
from app.models import UserCreate, UserPublic

router = APIRouter()


@router.post("/session", response_model=UserPublic)
def create_session(
    session: SessionDep,
    _: FrontendAuthDep,
    user_in: UserCreate,
) -> UserPublic:
    return get_or_create_user_from_api_key(session=session, user_create=user_in)
