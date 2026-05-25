from fastapi import APIRouter

from app.api.deps import CurrentUser, SessionDep
from app.crud import create_user
from app.models import UserCreate, UserPublic

router = APIRouter()


@router.get("/me", response_model=UserPublic)
def read_current_user(current_user: CurrentUser) -> UserPublic:
    return current_user


@router.post("/", response_model=UserPublic)
def register_user(session: SessionDep, user_in: UserCreate) -> UserPublic:
    return create_user(session=session, user_create=user_in)
