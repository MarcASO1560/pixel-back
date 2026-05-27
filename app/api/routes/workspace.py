from fastapi import APIRouter

from app.api.deps import CurrentUser, SessionDep
from app.crud import list_projects
from app.models import UserPublic, WorkspaceBootstrap

router = APIRouter()


@router.get("/", response_model=WorkspaceBootstrap)
def read_workspace(session: SessionDep, current_user: CurrentUser) -> WorkspaceBootstrap:
    return WorkspaceBootstrap(
        user=UserPublic.model_validate(current_user),
        projects=list_projects(session=session, owner_id=current_user.id),
    )
