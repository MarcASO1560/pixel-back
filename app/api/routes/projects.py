from fastapi import APIRouter, status

from app.api.deps import CurrentUser, SessionDep
from app.crud import (
    accept_project_share_link,
    create_project,
    create_project_folder,
    create_project_resource,
    delete_project_folder,
    delete_project,
    delete_project_resource,
    disable_project_share_link,
    get_or_create_project_share_link,
    get_project_resource,
    get_project_share_link,
    get_project_tree,
    leave_project,
    list_project_access,
    list_projects,
    remove_project_member,
    update_project,
    update_project_folder,
    update_project_member_role,
    update_project_resource,
)
from app.models import (
    ProjectAccessUserPublic,
    ProjectCreate,
    ProjectFolderCreate,
    ProjectFolderPublic,
    ProjectFolderUpdate,
    ProjectMemberUpdate,
    ProjectPublic,
    ProjectResourceCreate,
    ProjectResourceDetail,
    ProjectResourcePublic,
    ProjectResourceUpdate,
    ProjectShareLinkCreate,
    ProjectShareLinkPublic,
    ProjectTree,
    ProjectUpdate,
)

router = APIRouter()


@router.get("/", response_model=list[ProjectPublic])
def read_projects(session: SessionDep, current_user: CurrentUser) -> list[ProjectPublic]:
    return list_projects(session=session, user_id=current_user.id)


@router.post("/", response_model=ProjectPublic)
def add_project(
    session: SessionDep,
    current_user: CurrentUser,
    project_in: ProjectCreate,
) -> ProjectPublic:
    return create_project(session=session, owner_id=current_user.id, project_create=project_in)


@router.patch("/{project_id}", response_model=ProjectPublic)
def edit_project(
    session: SessionDep,
    current_user: CurrentUser,
    project_id: str,
    project_in: ProjectUpdate,
) -> ProjectPublic:
    return update_project(
        session=session,
        user_id=current_user.id,
        project_id=project_id,
        project_update=project_in,
    )


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_project(session: SessionDep, current_user: CurrentUser, project_id: str) -> None:
    delete_project(session=session, user_id=current_user.id, project_id=project_id)


@router.post("/share-links/{token}/accept", response_model=ProjectPublic)
def accept_share_link(
    session: SessionDep,
    current_user: CurrentUser,
    token: str,
) -> ProjectPublic:
    return accept_project_share_link(session=session, user_id=current_user.id, token=token)


@router.get("/{project_id}/tree", response_model=ProjectTree)
def read_project_tree(
    session: SessionDep,
    current_user: CurrentUser,
    project_id: str,
) -> ProjectTree:
    return get_project_tree(session=session, user_id=current_user.id, project_id=project_id)


@router.get("/{project_id}/access", response_model=list[ProjectAccessUserPublic])
def read_project_access(
    session: SessionDep,
    current_user: CurrentUser,
    project_id: str,
) -> list[ProjectAccessUserPublic]:
    return list_project_access(session=session, user_id=current_user.id, project_id=project_id)


@router.post("/{project_id}/share-link", response_model=ProjectShareLinkPublic)
def create_share_link(
    session: SessionDep,
    current_user: CurrentUser,
    project_id: str,
    share_link_in: ProjectShareLinkCreate | None = None,
) -> ProjectShareLinkPublic:
    return get_or_create_project_share_link(
        session=session,
        user_id=current_user.id,
        project_id=project_id,
        share_link_create=share_link_in,
    )


@router.get("/{project_id}/share-link", response_model=ProjectShareLinkPublic | None)
def read_share_link(
    session: SessionDep,
    current_user: CurrentUser,
    project_id: str,
) -> ProjectShareLinkPublic | None:
    return get_project_share_link(
        session=session,
        user_id=current_user.id,
        project_id=project_id,
    )


@router.delete("/{project_id}/share-link", status_code=status.HTTP_204_NO_CONTENT)
def delete_share_link(
    session: SessionDep,
    current_user: CurrentUser,
    project_id: str,
) -> None:
    disable_project_share_link(session=session, user_id=current_user.id, project_id=project_id)


@router.delete("/{project_id}/members/me", status_code=status.HTTP_204_NO_CONTENT)
def leave_shared_project(
    session: SessionDep,
    current_user: CurrentUser,
    project_id: str,
) -> None:
    leave_project(session=session, user_id=current_user.id, project_id=project_id)


@router.patch("/{project_id}/members/{member_user_id}", response_model=ProjectAccessUserPublic)
def edit_project_member(
    session: SessionDep,
    current_user: CurrentUser,
    project_id: str,
    member_user_id: str,
    member_in: ProjectMemberUpdate,
) -> ProjectAccessUserPublic:
    return update_project_member_role(
        session=session,
        user_id=current_user.id,
        project_id=project_id,
        member_user_id=member_user_id,
        member_update=member_in,
    )


@router.delete("/{project_id}/members/{member_user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project_member(
    session: SessionDep,
    current_user: CurrentUser,
    project_id: str,
    member_user_id: str,
) -> None:
    remove_project_member(
        session=session,
        user_id=current_user.id,
        project_id=project_id,
        member_user_id=member_user_id,
    )


@router.post("/{project_id}/folders", response_model=ProjectFolderPublic)
def add_project_folder(
    session: SessionDep,
    current_user: CurrentUser,
    project_id: str,
    folder_in: ProjectFolderCreate,
) -> ProjectFolderPublic:
    return create_project_folder(
        session=session,
        user_id=current_user.id,
        project_id=project_id,
        folder_create=folder_in,
    )


@router.post("/{project_id}/resources", response_model=ProjectResourcePublic)
def add_project_resource(
    session: SessionDep,
    current_user: CurrentUser,
    project_id: str,
    resource_in: ProjectResourceCreate,
) -> ProjectResourcePublic:
    return create_project_resource(
        session=session,
        user_id=current_user.id,
        project_id=project_id,
        resource_create=resource_in,
    )


@router.get("/{project_id}/resources/{resource_id}", response_model=ProjectResourceDetail)
def read_project_resource(
    session: SessionDep,
    current_user: CurrentUser,
    project_id: str,
    resource_id: str,
) -> ProjectResourceDetail:
    return get_project_resource(
        session=session,
        user_id=current_user.id,
        project_id=project_id,
        resource_id=resource_id,
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
        user_id=current_user.id,
        project_id=project_id,
        folder_id=folder_id,
        folder_update=folder_in,
    )


@router.delete("/{project_id}/folders/{folder_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_project_folder(
    session: SessionDep,
    current_user: CurrentUser,
    project_id: str,
    folder_id: str,
) -> None:
    delete_project_folder(
        session=session,
        user_id=current_user.id,
        project_id=project_id,
        folder_id=folder_id,
    )


@router.patch("/{project_id}/resources/{resource_id}", response_model=ProjectResourcePublic)
def edit_project_resource(
    session: SessionDep,
    current_user: CurrentUser,
    project_id: str,
    resource_id: str,
    resource_in: ProjectResourceUpdate,
) -> ProjectResourcePublic:
    return update_project_resource(
        session=session,
        user_id=current_user.id,
        project_id=project_id,
        resource_id=resource_id,
        resource_update=resource_in,
    )


@router.delete("/{project_id}/resources/{resource_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_project_resource(
    session: SessionDep,
    current_user: CurrentUser,
    project_id: str,
    resource_id: str,
) -> None:
    delete_project_resource(
        session=session,
        user_id=current_user.id,
        project_id=project_id,
        resource_id=resource_id,
    )
