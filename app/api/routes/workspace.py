from fastapi import APIRouter

from app.api.deps import CurrentUser, SessionDep
from app.crud import get_project_workspace, list_projects
from app.models import ProjectWorkspaceBootstrap, UserPublic, WorkspaceBootstrap

router = APIRouter()


@router.get("/", response_model=WorkspaceBootstrap)
def read_workspace(session: SessionDep, current_user: CurrentUser) -> WorkspaceBootstrap:
    return WorkspaceBootstrap(
        user=UserPublic.model_validate(current_user),
        projects=list_projects(session=session, user_id=current_user.id),
    )


@router.get("/projects/{project_id}", response_model=ProjectWorkspaceBootstrap)
def read_project_workspace(
    session: SessionDep,
    current_user: CurrentUser,
    project_id: str,
) -> ProjectWorkspaceBootstrap:
    return get_project_workspace(
        session=session,
        user=current_user,
        project_id=project_id,
    )
