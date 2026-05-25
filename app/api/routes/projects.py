from fastapi import APIRouter

from app.api.deps import CurrentUser, SessionDep
from app.crud import (
    create_project,
    create_project_folder,
    get_project_tree,
    list_projects,
    update_project_folder,
)
from app.models import (
    ProjectCreate,
    ProjectFolderCreate,
    ProjectFolderPublic,
    ProjectFolderUpdate,
    ProjectPublic,
    ProjectTree,
)

router = APIRouter()


@router.get("/", response_model=list[ProjectPublic])
def read_projects(session: SessionDep, current_user: CurrentUser) -> list[ProjectPublic]:
    return list_projects(session=session, owner_id=current_user.id)


@router.post("/", response_model=ProjectPublic)
def add_project(
    session: SessionDep,
    current_user: CurrentUser,
    project_in: ProjectCreate,
) -> ProjectPublic:
    return create_project(session=session, owner_id=current_user.id, project_create=project_in)


@router.get("/{project_id}/tree", response_model=ProjectTree)
def read_project_tree(
    session: SessionDep,
    current_user: CurrentUser,
    project_id: str,
) -> ProjectTree:
    return get_project_tree(session=session, owner_id=current_user.id, project_id=project_id)


@router.post("/{project_id}/folders", response_model=ProjectFolderPublic)
def add_project_folder(
    session: SessionDep,
    current_user: CurrentUser,
    project_id: str,
    folder_in: ProjectFolderCreate,
) -> ProjectFolderPublic:
    return create_project_folder(
        session=session,
        owner_id=current_user.id,
        project_id=project_id,
        folder_create=folder_in,
    )


@router.patch("/{project_id}/folders/{folder_id}", response_model=ProjectFolderPublic)
def edit_project_folder(
    session: SessionDep,
    current_user: CurrentUser,
    project_id: str,
    folder_id: str,
    folder_in: ProjectFolderUpdate,
) -> ProjectFolderPublic:
    return update_project_folder(
        session=session,
        owner_id=current_user.id,
        project_id=project_id,
        folder_id=folder_id,
        folder_update=folder_in,
    )
