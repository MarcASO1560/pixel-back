from fastapi import APIRouter

from app.api.deps import CurrentUser, SessionDep
from app.crud import update_current_user
from app.models import UserPublic, UserUpdate

router = APIRouter()


@router.get("/me", response_model=UserPublic)
def read_current_user(current_user: CurrentUser) -> UserPublic:
    return current_user


@router.patch("/me", response_model=UserPublic)
def update_me(
    session: SessionDep,
    current_user: CurrentUser,
    user_in: UserUpdate,
) -> UserPublic:
    return update_current_user(
        session=session,
        current_user=current_user,
        user_update=user_in,
    )
