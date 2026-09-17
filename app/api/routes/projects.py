from fastapi import APIRouter, Query, status

from app.api.deps import CurrentUser, SessionDep
from app.crud import (
    accept_project_share_link,
    block_project_user,
    create_project,
    create_project_folder,
    create_project_resource,
    delete_project,
    delete_project_folder,
    delete_project_resource,
    disable_project_share_link,
    get_or_create_project_share_link,
    get_project_resource,
    get_project_share_link,
    get_project_tree,
    get_resource_editor_state,
    leave_project,
    list_project_access,
    list_project_blocked_users,
    list_projects,
    remove_project_member,
    unblock_project_user,
    update_project,
    update_project_folder,
    update_project_member_role,
    update_project_resource,
    upsert_resource_editor_state,
)
from app.image_operations import (
    ImageOperationRequest,
    ImageOperationResponse,
    ImageOperationState,
    get_image_operation_state,
    submit_image_operation,
)
from app.models import (
    ProjectAccessUserPublic,
    ProjectBlockedUserPublic,
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
    ResourceEditorStatePublic,
    ResourceEditorStateUpdate,
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


@router.get("/{project_id}/blocked-users", response_model=list[ProjectBlockedUserPublic])
def read_project_blocked_users(
    session: SessionDep,
    current_user: CurrentUser,
    project_id: str,
) -> list[ProjectBlockedUserPublic]:
    return list_project_blocked_users(
        session=session, user_id=current_user.id, project_id=project_id
    )


@router.post(
    "/{project_id}/blocked-users/{blocked_user_id}", response_model=ProjectBlockedUserPublic
)
def add_project_blocked_user(
    session: SessionDep,
    current_user: CurrentUser,
    project_id: str,
    blocked_user_id: str,
) -> ProjectBlockedUserPublic:
    return block_project_user(
        session=session,
        user_id=current_user.id,
        project_id=project_id,
        blocked_user_id=blocked_user_id,
    )


@router.delete(
    "/{project_id}/blocked-users/{blocked_user_id}", status_code=status.HTTP_204_NO_CONTENT
)
def delete_project_blocked_user(
    session: SessionDep,
    current_user: CurrentUser,
    project_id: str,
    blocked_user_id: str,
) -> None:
    unblock_project_user(
        session=session,
        user_id=current_user.id,
        project_id=project_id,
        blocked_user_id=blocked_user_id,
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


@router.get(
    "/{project_id}/resources/{resource_id}/editor-state",
    response_model=ResourceEditorStatePublic | None,
)
def read_resource_editor_state(
    session: SessionDep,
    current_user: CurrentUser,
    project_id: str,
    resource_id: str,
) -> ResourceEditorStatePublic | None:
    return get_resource_editor_state(
        session=session,
        user_id=current_user.id,
        project_id=project_id,
        resource_id=resource_id,
    )


@router.post(
    "/{project_id}/resources/{resource_id}/image-operations",
    response_model=ImageOperationResponse,
)
def apply_image_operation(
    session: SessionDep,
    current_user: CurrentUser,
    project_id: str,
    resource_id: str,
    operation: ImageOperationRequest,
) -> ImageOperationResponse:
    return submit_image_operation(
        session=session,
        user_id=current_user.id,
        project_id=project_id,
        resource_id=resource_id,
        packet=operation,
    )


@router.get(
    "/{project_id}/resources/{resource_id}/image-operations",
    response_model=ImageOperationState,
)
def read_image_operation_state(
    session: SessionDep,
    current_user: CurrentUser,
    project_id: str,
    resource_id: str,
    since_revision: int = Query(default=0, ge=0),
) -> ImageOperationState:
    return get_image_operation_state(
        session=session,
        user_id=current_user.id,
        project_id=project_id,
        resource_id=resource_id,
        since_revision=since_revision,
    )


@router.put(
    "/{project_id}/resources/{resource_id}/editor-state",
    response_model=ResourceEditorStatePublic,
)
def save_resource_editor_state(
    session: SessionDep,
    current_user: CurrentUser,
    project_id: str,
    resource_id: str,
    state_in: ResourceEditorStateUpdate,
) -> ResourceEditorStatePublic:
    return upsert_resource_editor_state(
        session=session,
        user_id=current_user.id,
        project_id=project_id,
        resource_id=resource_id,
        state_update=state_in,
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
