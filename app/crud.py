from datetime import datetime, timedelta
from secrets import token_urlsafe
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import delete
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, select

from app.core.config import settings
from app.core.security import (
    create_password_reset_token,
    get_password_hash,
    get_password_reset_token_hash,
    verify_password,
)
from app.models import (
    PasswordCredential,
    PasswordResetConfirmCreate,
    PasswordResetRequestCreate,
    PasswordResetToken,
    Project,
    ProjectAccessRole,
    ProjectAccessUserPublic,
    ProjectCreate,
    ProjectFolder,
    ProjectFolderCreate,
    ProjectFolderUpdate,
    ProjectMember,
    ProjectMemberUpdate,
    ProjectPublic,
    ProjectResource,
    ProjectResourceCreate,
    ProjectResourceUpdate,
    ProjectShareLink,
    ProjectShareLinkCreate,
    ProjectShareLinkPublic,
    ProjectTree,
    ProjectUpdate,
    RealtimeEventLog,
    ResourceExport,
    ResourceRevision,
    User,
    UserCreate,
    UserRegistrationCreate,
    UserUpdate,
)
from app.realtime import realtime_broker
from app.time import utc_now

PASSWORD_RESET_TOKEN_MINUTES = 30
PROJECT_SHARE_TOKEN_BYTES = 24
PROJECT_ACCESS_ROLES = {role.value for role in ProjectAccessRole}
PROJECT_SHARE_LINK_ROLES = {ProjectAccessRole.viewer.value, ProjectAccessRole.editor.value}
PROJECT_EDIT_ROLES = {ProjectAccessRole.editor.value, ProjectAccessRole.owner.value}
PROJECT_MANAGE_ROLES = {ProjectAccessRole.owner.value}
REALTIME_EVENT_RETENTION_HOURS = 24


def normalize_email(email: str) -> str:
    return email.strip().lower()


def normalize_username(username: str) -> str:
    return username.strip().lower()


def get_user_by_email(*, session: Session, email: str) -> User | None:
    statement = select(User).where(User.email == normalize_email(email))
    return session.exec(statement).first()


def get_user_by_username(*, session: Session, username: str) -> User | None:
    statement = select(User).where(User.username == normalize_username(username))
    return session.exec(statement).first()


def get_project_access_count(*, session: Session, project_id: UUID) -> int:
    member_ids = session.exec(
        select(ProjectMember.user_id).where(ProjectMember.project_id == project_id),
    ).all()
    return 1 + len(member_ids)


def project_to_public(*, session: Session, project: Project, access_role: str) -> ProjectPublic:
    return ProjectPublic.model_validate(
        project,
        update={
            "access_role": access_role,
            "access_count": get_project_access_count(session=session, project_id=project.id),
        },
    )


def list_project_user_ids(*, session: Session, project_id: UUID) -> set[UUID]:
    project = session.get(Project, project_id)
    if not project:
        return set()

    member_user_ids = session.exec(
        select(ProjectMember.user_id).where(ProjectMember.project_id == project_id),
    ).all()
    return {project.owner_id, *member_user_ids}


def publish_project_event(
    *,
    session: Session,
    project_id: UUID,
    event: str,
    actor_id: UUID,
    user_ids: set[UUID] | None = None,
    extra: dict[str, object] | None = None,
) -> None:
    target_user_ids = (
        user_ids
        if user_ids is not None
        else list_project_user_ids(session=session, project_id=project_id)
    )
    data = {
        "project_id": str(project_id),
        "actor_id": str(actor_id),
        **(extra or {}),
    }

    try:
        session.add_all(
            [
                RealtimeEventLog(
                    user_id=target_user_id,
                    event=event,
                    data=data,
                )
                for target_user_id in target_user_ids
            ],
        )
        session.exec(
            delete(RealtimeEventLog).where(
                RealtimeEventLog.created_at
                < utc_now() - timedelta(hours=REALTIME_EVENT_RETENTION_HOURS),
            ),
        )
        session.commit()
    except SQLAlchemyError:
        session.rollback()

    realtime_broker.publish(
        user_ids=target_user_ids,
        event=event,
        data=data,
    )


def project_share_link_to_public(*, link: ProjectShareLink) -> ProjectShareLinkPublic:
    return ProjectShareLinkPublic(
        project_id=link.project_id,
        token=link.token,
        url=f"{settings.FRONTEND_URL.rstrip('/')}/share/{link.token}",
        role=normalize_project_share_link_role(link.role),
        created_at=link.created_at,
        updated_at=link.updated_at,
    )


def normalize_project_role(role: str) -> str:
    normalized_role = role.strip().lower()
    if normalized_role not in PROJECT_ACCESS_ROLES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid project role",
        )
    return normalized_role


def normalize_project_share_role(role: str) -> str:
    normalized_role = normalize_project_role(role)
    if normalized_role not in PROJECT_SHARE_LINK_ROLES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Share links can only grant viewer or editor access",
        )
    return normalized_role


def normalize_project_share_link_role(role: str) -> str:
    normalized_role = role.strip().lower()
    return (
        normalized_role
        if normalized_role in PROJECT_SHARE_LINK_ROLES
        else ProjectAccessRole.editor.value
    )


def get_project_member(
    *,
    session: Session,
    project_id: UUID,
    user_id: UUID,
) -> ProjectMember | None:
    statement = select(ProjectMember).where(
        ProjectMember.project_id == project_id,
        ProjectMember.user_id == user_id,
    )
    return session.exec(statement).first()


def get_project_access_role(
    *,
    session: Session,
    project: Project,
    user_id: UUID,
) -> str | None:
    if project.owner_id == user_id:
        return ProjectAccessRole.owner.value

    member = get_project_member(session=session, project_id=project.id, user_id=user_id)
    return member.role if member else None


def project_access_user_to_public(
    *,
    user: User,
    role: str,
    is_owner: bool,
    joined_at: datetime | None,
) -> ProjectAccessUserPublic:
    return ProjectAccessUserPublic(
        id=user.id,
        username=user.username,
        email=user.email,
        avatar_url=user.avatar_url,
        avatar_pixel_art=user.avatar_pixel_art,
        role=role,
        is_owner=is_owner,
        joined_at=joined_at,
    )


def create_user(*, session: Session, user_create: UserCreate) -> User:
    existing_user = get_user_by_email(session=session, email=user_create.email)
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A user with this email already exists",
        )

    username = normalize_username(user_create.username) if user_create.username else None
    if username and get_user_by_username(session=session, username=username):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A user with this username already exists",
        )

    user = User(
        username=username,
        email=normalize_email(user_create.email),
        avatar_url=user_create.avatar_url,
        is_admin=False,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def upsert_user_from_identity(*, session: Session, user_create: UserCreate) -> User:
    user = get_user_by_email(session=session, email=user_create.email)
    if user:
        if user_create.username:
            user.username = normalize_username(user_create.username)
        user.avatar_url = user_create.avatar_url
        user.updated_at = utc_now()
        session.add(user)
        session.commit()
        session.refresh(user)
        return user

    return create_user(session=session, user_create=user_create)


def create_user_with_password(
    *,
    session: Session,
    user_create: UserRegistrationCreate,
) -> User:
    if user_create.password != user_create.password_confirmation:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Passwords do not match",
        )

    username = normalize_username(user_create.username)
    email = normalize_email(user_create.email)

    if get_user_by_username(session=session, username=username):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A user with this username already exists",
        )

    if get_user_by_email(session=session, email=email):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A user with this email already exists",
        )

    user = User(
        username=username,
        email=email,
        is_admin=False,
    )
    session.add(user)
    session.flush()

    password_credential = PasswordCredential(
        user_id=user.id,
        password_hash=get_password_hash(user_create.password),
    )
    session.add(password_credential)
    session.commit()
    session.refresh(user)
    return user


def authenticate_user_with_password(
    *,
    session: Session,
    email: str,
    password: str,
) -> User | None:
    user = get_user_by_email(session=session, email=email)
    if not user:
        return None

    password_credential = session.get(PasswordCredential, user.id)
    if not password_credential:
        return None

    if not verify_password(password, password_credential.password_hash):
        return None

    return user


def update_current_user(
    *,
    session: Session,
    current_user: User,
    user_update: UserUpdate,
) -> User:
    user_data = user_update.model_dump(exclude_unset=True)

    if "username" in user_data:
        username = normalize_username(user_data["username"]) if user_data["username"] else None
        if username:
            existing_user = get_user_by_username(session=session, username=username)
            if existing_user and existing_user.id != current_user.id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="A user with this username already exists",
                )
        current_user.username = username

    if "avatar_pixel_art" in user_data:
        current_user.avatar_pixel_art = user_data["avatar_pixel_art"]

    current_user.updated_at = utc_now()
    session.add(current_user)
    session.commit()
    session.refresh(current_user)
    return current_user


def create_password_reset_request(
    *,
    session: Session,
    reset_request: PasswordResetRequestCreate,
) -> str | None:
    user = get_user_by_email(session=session, email=reset_request.email)
    if not user:
        return None

    token = create_password_reset_token()
    reset_token = PasswordResetToken(
        user_id=user.id,
        token_hash=get_password_reset_token_hash(token),
        expires_at=utc_now() + timedelta(minutes=PASSWORD_RESET_TOKEN_MINUTES),
    )
    session.add(reset_token)
    session.commit()
    return token


def confirm_password_reset(
    *,
    session: Session,
    reset_confirm: PasswordResetConfirmCreate,
) -> User:
    if reset_confirm.password != reset_confirm.password_confirmation:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Passwords do not match",
        )

    token_hash = get_password_reset_token_hash(reset_confirm.token)
    statement = select(PasswordResetToken).where(PasswordResetToken.token_hash == token_hash)
    reset_token = session.exec(statement).first()

    if (
        not reset_token
        or reset_token.used_at is not None
        or reset_token.expires_at <= utc_now()
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired reset token",
        )

    password_credential = session.get(PasswordCredential, reset_token.user_id)
    if password_credential:
        password_credential.password_hash = get_password_hash(reset_confirm.password)
        password_credential.updated_at = utc_now()
    else:
        password_credential = PasswordCredential(
            user_id=reset_token.user_id,
            password_hash=get_password_hash(reset_confirm.password),
        )

    reset_token.used_at = utc_now()
    session.add(password_credential)
    session.add(reset_token)
    session.commit()

    user = session.get(User, reset_token.user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    return user


def list_projects(*, session: Session, user_id: UUID) -> list[ProjectPublic]:
    owned_statement = select(Project).where(Project.owner_id == user_id)
    shared_statement = (
        select(Project, ProjectMember)
        .join(ProjectMember, ProjectMember.project_id == Project.id)
        .where(ProjectMember.user_id == user_id)
    )

    projects_by_id: dict[UUID, ProjectPublic] = {
        project.id: project_to_public(session=session, project=project, access_role="owner")
        for project in session.exec(owned_statement).all()
    }

    for project, member in session.exec(shared_statement).all():
        if project.id not in projects_by_id:
            projects_by_id[project.id] = project_to_public(
                session=session,
                project=project,
                access_role=member.role,
            )

    return sorted(
        projects_by_id.values(),
        key=lambda project: project.updated_at,
        reverse=True,
    )


def create_project(*, session: Session, owner_id: UUID, project_create: ProjectCreate) -> Project:
    project = Project.model_validate(project_create, update={"owner_id": owner_id})
    session.add(project)
    session.commit()
    session.refresh(project)
    publish_project_event(
        session=session,
        project_id=project.id,
        event="workspace.updated",
        actor_id=owner_id,
        user_ids={owner_id},
    )
    return project


def update_project(
    *,
    session: Session,
    user_id: UUID,
    project_id: str,
    project_update: ProjectUpdate,
) -> ProjectPublic:
    project = get_project_or_404(
        session=session,
        user_id=user_id,
        project_id=project_id,
        required_roles=PROJECT_EDIT_ROLES,
    )
    project_data = project_update.model_dump(exclude_unset=True)

    for field, value in project_data.items():
        setattr(project, field, value)

    project.updated_at = utc_now()
    session.add(project)
    session.commit()
    session.refresh(project)
    publish_project_event(
        session=session,
        project_id=project.id,
        event="project.updated",
        actor_id=user_id,
    )
    publish_project_event(
        session=session,
        project_id=project.id,
        event="workspace.updated",
        actor_id=user_id,
    )
    access_role = get_project_access_role(session=session, project=project, user_id=user_id)
    if not access_role:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return project_to_public(session=session, project=project, access_role=access_role)


def delete_project(*, session: Session, user_id: UUID, project_id: str) -> None:
    project = get_project_or_404(
        session=session,
        user_id=user_id,
        project_id=project_id,
        required_roles=PROJECT_MANAGE_ROLES,
    )
    if get_project_access_count(session=session, project_id=project.id) > 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Projects with other people cannot be deleted",
        )

    user_ids = list_project_user_ids(session=session, project_id=project.id)
    project_resource_ids = select(ProjectResource.id).where(
        ProjectResource.project_id == project.id
    )
    session.exec(delete(ResourceExport).where(ResourceExport.resource_id.in_(project_resource_ids)))
    session.exec(delete(ResourceRevision).where(ResourceRevision.resource_id.in_(project_resource_ids)))
    session.exec(delete(ProjectResource).where(ProjectResource.project_id == project.id))
    session.exec(delete(ProjectFolder).where(ProjectFolder.project_id == project.id))
    session.exec(delete(ProjectMember).where(ProjectMember.project_id == project.id))
    session.exec(delete(ProjectShareLink).where(ProjectShareLink.project_id == project.id))
    session.delete(project)
    session.commit()
    publish_project_event(
        session=session,
        project_id=project.id,
        event="project.deleted",
        actor_id=user_id,
        user_ids=user_ids,
    )


def parse_project_id_or_404(project_id: str) -> UUID:
    try:
        return UUID(project_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        ) from None


def get_owned_project_or_404(
    *,
    session: Session,
    owner_id: UUID,
    project_id: str,
) -> Project:
    parsed_project_id = parse_project_id_or_404(project_id)

    project = session.get(Project, parsed_project_id)
    if not project or project.owner_id != owner_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return project


def get_project_or_404(
    *,
    session: Session,
    user_id: UUID,
    project_id: str,
    required_roles: set[str] | None = None,
) -> Project:
    parsed_project_id = parse_project_id_or_404(project_id)

    project = session.get(Project, parsed_project_id)
    if not project:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    access_role = get_project_access_role(session=session, project=project, user_id=user_id)
    if access_role and (required_roles is None or access_role in required_roles):
        return project

    if access_role:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient project role",
        )

    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")


def get_project_tree(*, session: Session, user_id: UUID, project_id: str) -> ProjectTree:
    project = get_project_or_404(session=session, user_id=user_id, project_id=project_id)

    folders_statement = (
        select(ProjectFolder)
        .where(ProjectFolder.project_id == project.id)
        .order_by(ProjectFolder.position, ProjectFolder.name)
    )
    resources_statement = (
        select(ProjectResource)
        .where(ProjectResource.project_id == project.id)
        .order_by(ProjectResource.position, ProjectResource.name)
    )

    return ProjectTree(
        folders=list(session.exec(folders_statement).all()),
        resources=list(session.exec(resources_statement).all()),
    )


def get_project_resource(
    *,
    session: Session,
    user_id: UUID,
    project_id: str,
    resource_id: str,
) -> ProjectResource:
    project = get_project_or_404(session=session, user_id=user_id, project_id=project_id)

    try:
        parsed_resource_id = UUID(resource_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Resource not found",
        ) from None

    resource = session.get(ProjectResource, parsed_resource_id)
    if not resource or resource.project_id != project.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found")

    return resource


def list_project_access(
    *,
    session: Session,
    user_id: UUID,
    project_id: str,
) -> list[ProjectAccessUserPublic]:
    project = get_project_or_404(session=session, user_id=user_id, project_id=project_id)
    owner = session.get(User, project.owner_id)
    users: list[ProjectAccessUserPublic] = []

    if owner:
        users.append(
            project_access_user_to_public(
                user=owner,
                role="owner",
                is_owner=True,
                joined_at=project.created_at,
            ),
        )

    statement = (
        select(User, ProjectMember)
        .join(ProjectMember, ProjectMember.user_id == User.id)
        .where(ProjectMember.project_id == project.id)
        .order_by(ProjectMember.created_at, User.username, User.email)
    )

    for member_user, member in session.exec(statement).all():
        users.append(
            project_access_user_to_public(
                user=member_user,
                role=member.role,
                is_owner=False,
                joined_at=member.created_at,
            ),
        )

    return users


def generate_project_share_token(*, session: Session) -> str:
    while True:
        token = token_urlsafe(PROJECT_SHARE_TOKEN_BYTES)
        statement = select(ProjectShareLink).where(ProjectShareLink.token == token)
        if not session.exec(statement).first():
            return token


def get_or_create_project_share_link(
    *,
    session: Session,
    user_id: UUID,
    project_id: str,
    share_link_create: ProjectShareLinkCreate | None = None,
) -> ProjectShareLinkPublic:
    project = get_project_or_404(
        session=session,
        user_id=user_id,
        project_id=project_id,
        required_roles=PROJECT_MANAGE_ROLES,
    )
    role = normalize_project_share_role(
        share_link_create.role
        if share_link_create
        else ProjectAccessRole.editor.value
    )
    link = session.get(ProjectShareLink, project.id)
    did_change = False

    if not link:
        link = ProjectShareLink(
            project_id=project.id,
            token=generate_project_share_token(session=session),
            role=role,
        )
        session.add(link)
        session.commit()
        session.refresh(link)
        did_change = True
    elif link.role != role:
        link.role = role
        link.updated_at = utc_now()
        session.add(link)
        session.commit()
        session.refresh(link)
        did_change = True

    if did_change:
        publish_project_event(
            session=session,
            project_id=project.id,
            event="project.share.updated",
            actor_id=user_id,
        )

    return project_share_link_to_public(link=link)


def get_project_share_link(
    *,
    session: Session,
    user_id: UUID,
    project_id: str,
) -> ProjectShareLinkPublic | None:
    project = get_project_or_404(
        session=session,
        user_id=user_id,
        project_id=project_id,
        required_roles=PROJECT_MANAGE_ROLES,
    )
    link = session.get(ProjectShareLink, project.id)
    return project_share_link_to_public(link=link) if link else None


def disable_project_share_link(
    *,
    session: Session,
    user_id: UUID,
    project_id: str,
) -> None:
    project = get_project_or_404(
        session=session,
        user_id=user_id,
        project_id=project_id,
        required_roles=PROJECT_MANAGE_ROLES,
    )
    link = session.get(ProjectShareLink, project.id)
    if link:
        session.delete(link)
        session.commit()
        publish_project_event(
            session=session,
            project_id=project.id,
            event="project.share.updated",
            actor_id=user_id,
        )


def accept_project_share_link(
    *,
    session: Session,
    user_id: UUID,
    token: str,
) -> ProjectPublic:
    statement = select(ProjectShareLink).where(ProjectShareLink.token == token)
    link = session.exec(statement).first()
    if not link:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Share link not found",
        )

    project = session.get(Project, link.project_id)
    if not project:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    if project.owner_id == user_id:
        return project_to_public(session=session, project=project, access_role="owner")

    member = get_project_member(session=session, project_id=project.id, user_id=user_id)
    if not member:
        member = ProjectMember(
            project_id=project.id,
            user_id=user_id,
            role=normalize_project_share_link_role(link.role),
        )
        session.add(member)
        session.commit()
        session.refresh(member)
        publish_project_event(
            session=session,
            project_id=project.id,
            event="project.access.updated",
            actor_id=user_id,
        )
        publish_project_event(
            session=session,
            project_id=project.id,
            event="workspace.updated",
            actor_id=user_id,
        )

    return project_to_public(session=session, project=project, access_role=member.role)


def update_project_member_role(
    *,
    session: Session,
    user_id: UUID,
    project_id: str,
    member_user_id: str,
    member_update: ProjectMemberUpdate,
) -> ProjectAccessUserPublic:
    project = get_project_or_404(
        session=session,
        user_id=user_id,
        project_id=project_id,
        required_roles=PROJECT_MANAGE_ROLES,
    )
    target_user_id = parse_project_id_or_404(member_user_id)
    target_user = session.get(User, target_user_id)
    if not target_user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project member not found",
        )

    next_role = normalize_project_role(member_update.role)
    if project.owner_id == target_user_id:
        if next_role == ProjectAccessRole.owner.value:
            return project_access_user_to_public(
                user=target_user,
                role=ProjectAccessRole.owner.value,
                is_owner=True,
                joined_at=project.created_at,
            )

        owner_members = session.exec(
            select(ProjectMember)
            .where(
                ProjectMember.project_id == project.id,
                ProjectMember.role == ProjectAccessRole.owner.value,
            )
            .order_by(ProjectMember.created_at),
        ).all()
        next_owner = next(
            (member for member in owner_members if member.user_id == user_id),
            owner_members[0] if owner_members else None,
        )
        if not next_owner:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Another owner is required before changing this owner role",
            )

        project.owner_id = next_owner.user_id
        project.updated_at = utc_now()
        replacement_member = ProjectMember(
            project_id=project.id,
            user_id=target_user_id,
            role=next_role,
        )
        session.add(project)
        session.delete(next_owner)
        session.add(replacement_member)
        session.commit()
        session.refresh(replacement_member)
        publish_project_event(
            session=session,
            project_id=project.id,
            event="project.access.updated",
            actor_id=user_id,
        )
        publish_project_event(
            session=session,
            project_id=project.id,
            event="workspace.updated",
            actor_id=user_id,
        )
        return project_access_user_to_public(
            user=target_user,
            role=replacement_member.role,
            is_owner=False,
            joined_at=replacement_member.created_at,
        )

    member = get_project_member(session=session, project_id=project.id, user_id=target_user_id)
    if not member:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project member not found",
        )

    member.role = next_role
    member.updated_at = utc_now()
    session.add(member)
    session.commit()
    session.refresh(member)
    publish_project_event(
        session=session,
        project_id=project.id,
        event="project.access.updated",
        actor_id=user_id,
    )
    publish_project_event(
        session=session,
        project_id=project.id,
        event="workspace.updated",
        actor_id=user_id,
    )
    return project_access_user_to_public(
        user=target_user,
        role=member.role,
        is_owner=False,
        joined_at=member.created_at,
    )


def remove_project_member(
    *,
    session: Session,
    user_id: UUID,
    project_id: str,
    member_user_id: str,
) -> None:
    project = get_project_or_404(
        session=session,
        user_id=user_id,
        project_id=project_id,
        required_roles=PROJECT_MANAGE_ROLES,
    )
    target_user_id = parse_project_id_or_404(member_user_id)
    member = get_project_member(session=session, project_id=project.id, user_id=target_user_id)
    if not member:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project member not found",
        )

    user_ids = list_project_user_ids(session=session, project_id=project.id)
    session.delete(member)
    session.commit()
    publish_project_event(
        session=session,
        project_id=project.id,
        event="project.access.updated",
        actor_id=user_id,
        user_ids=user_ids,
    )
    publish_project_event(
        session=session,
        project_id=project.id,
        event="workspace.updated",
        actor_id=user_id,
        user_ids=user_ids,
    )


def leave_project(
    *,
    session: Session,
    user_id: UUID,
    project_id: str,
) -> None:
    project = get_project_or_404(session=session, user_id=user_id, project_id=project_id)
    if get_project_access_count(session=session, project_id=project.id) <= 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Projects with only one person must be deleted",
        )

    user_ids = list_project_user_ids(session=session, project_id=project.id)
    if project.owner_id == user_id:
        members = session.exec(
            select(ProjectMember)
            .where(ProjectMember.project_id == project.id)
            .order_by(ProjectMember.created_at),
        ).all()
        next_owner = next(
            (member for member in members if member.role == ProjectAccessRole.owner.value),
            members[0] if members else None,
        )
        if not next_owner:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Projects with only one person must be deleted",
            )

        project.owner_id = next_owner.user_id
        project.updated_at = utc_now()
        session.add(project)
        session.delete(next_owner)
        session.commit()
        publish_project_event(
            session=session,
            project_id=project.id,
            event="project.access.updated",
            actor_id=user_id,
            user_ids=user_ids,
        )
        publish_project_event(
            session=session,
            project_id=project.id,
            event="workspace.updated",
            actor_id=user_id,
            user_ids=user_ids,
        )
        return

    member = get_project_member(session=session, project_id=project.id, user_id=user_id)
    if not member:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project member not found",
        )

    session.delete(member)
    session.commit()
    publish_project_event(
        session=session,
        project_id=project.id,
        event="project.access.updated",
        actor_id=user_id,
        user_ids=user_ids,
    )
    publish_project_event(
        session=session,
        project_id=project.id,
        event="workspace.updated",
        actor_id=user_id,
        user_ids=user_ids,
    )


def create_project_folder(
    *,
    session: Session,
    user_id: UUID,
    project_id: str,
    folder_create: ProjectFolderCreate,
) -> ProjectFolder:
    project = get_project_or_404(
        session=session,
        user_id=user_id,
        project_id=project_id,
        required_roles=PROJECT_EDIT_ROLES,
    )

    if folder_create.parent_id:
        parent_folder = session.get(ProjectFolder, folder_create.parent_id)
        if not parent_folder or parent_folder.project_id != project.id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Parent folder not found in this project",
            )

    folder = ProjectFolder.model_validate(folder_create, update={"project_id": project.id})
    session.add(folder)
    session.commit()
    session.refresh(folder)
    publish_project_event(
        session=session,
        project_id=project.id,
        event="project.updated",
        actor_id=user_id,
    )
    return folder


def create_project_resource(
    *,
    session: Session,
    user_id: UUID,
    project_id: str,
    resource_create: ProjectResourceCreate,
) -> ProjectResource:
    project = get_project_or_404(
        session=session,
        user_id=user_id,
        project_id=project_id,
        required_roles=PROJECT_EDIT_ROLES,
    )

    if resource_create.folder_id:
        parent_folder = session.get(ProjectFolder, resource_create.folder_id)
        if not parent_folder or parent_folder.project_id != project.id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Folder not found in this project",
            )

    resource = ProjectResource.model_validate(resource_create, update={"project_id": project.id})
    session.add(resource)
    session.commit()
    session.refresh(resource)
    publish_project_event(
        session=session,
        project_id=project.id,
        event="project.updated",
        actor_id=user_id,
    )
    return resource


def update_project_folder(
    *,
    session: Session,
    user_id: UUID,
    project_id: str,
    folder_id: str,
    folder_update: ProjectFolderUpdate,
) -> ProjectFolder:
    project = get_project_or_404(
        session=session,
        user_id=user_id,
        project_id=project_id,
        required_roles=PROJECT_EDIT_ROLES,
    )

    try:
        parsed_folder_id = UUID(folder_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Folder not found",
        ) from None

    folder = session.get(ProjectFolder, parsed_folder_id)
    if not folder or folder.project_id != project.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Folder not found")

    folder_data = folder_update.model_dump(exclude_unset=True)
    if "parent_id" in folder_data and folder_data["parent_id"] is not None:
        parent_folder = session.get(ProjectFolder, folder_data["parent_id"])
        if not parent_folder or parent_folder.project_id != project.id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Parent folder not found in this project",
            )

        if parent_folder.id == folder.id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="A folder cannot be moved inside itself",
            )

        ancestor_folder = parent_folder
        visited_folder_ids: set[UUID] = set()
        while ancestor_folder.parent_id:
            if ancestor_folder.parent_id == folder.id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="A folder cannot be moved inside its own descendant",
                )

            if ancestor_folder.parent_id in visited_folder_ids:
                break

            visited_folder_ids.add(ancestor_folder.parent_id)
            next_ancestor = session.get(ProjectFolder, ancestor_folder.parent_id)
            if not next_ancestor or next_ancestor.project_id != project.id:
                break

            ancestor_folder = next_ancestor

    for field, value in folder_data.items():
        setattr(folder, field, value)

    folder.updated_at = utc_now()
    session.add(folder)
    session.commit()
    session.refresh(folder)
    publish_project_event(
        session=session,
        project_id=project.id,
        event="project.updated",
        actor_id=user_id,
    )
    return folder


def update_project_resource(
    *,
    session: Session,
    user_id: UUID,
    project_id: str,
    resource_id: str,
    resource_update: ProjectResourceUpdate,
) -> ProjectResource:
    project = get_project_or_404(
        session=session,
        user_id=user_id,
        project_id=project_id,
        required_roles=PROJECT_EDIT_ROLES,
    )

    try:
        parsed_resource_id = UUID(resource_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Resource not found",
        ) from None

    resource = session.get(ProjectResource, parsed_resource_id)
    if not resource or resource.project_id != project.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found")

    resource_data = resource_update.model_dump(exclude_unset=True)
    if "folder_id" in resource_data and resource_data["folder_id"] is not None:
        parent_folder = session.get(ProjectFolder, resource_data["folder_id"])
        if not parent_folder or parent_folder.project_id != project.id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Folder not found in this project",
            )

    for field, value in resource_data.items():
        setattr(resource, field, value)

    resource.updated_at = utc_now()
    session.add(resource)
    session.commit()
    session.refresh(resource)
    publish_project_event(
        session=session,
        project_id=project.id,
        event="project.updated",
        actor_id=user_id,
    )
    return resource


def delete_project_resource(
    *,
    session: Session,
    user_id: UUID,
    project_id: str,
    resource_id: str,
) -> None:
    project = get_project_or_404(
        session=session,
        user_id=user_id,
        project_id=project_id,
        required_roles=PROJECT_EDIT_ROLES,
    )

    try:
        parsed_resource_id = UUID(resource_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Resource not found",
        ) from None

    resource = session.get(ProjectResource, parsed_resource_id)
    if not resource or resource.project_id != project.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found")

    session.exec(delete(ResourceExport).where(ResourceExport.resource_id == resource.id))
    session.exec(delete(ResourceRevision).where(ResourceRevision.resource_id == resource.id))
    session.delete(resource)
    session.commit()
    publish_project_event(
        session=session,
        project_id=project.id,
        event="project.updated",
        actor_id=user_id,
    )


def delete_project_folder(
    *,
    session: Session,
    user_id: UUID,
    project_id: str,
    folder_id: str,
) -> None:
    project = get_project_or_404(
        session=session,
        user_id=user_id,
        project_id=project_id,
        required_roles=PROJECT_EDIT_ROLES,
    )

    try:
        parsed_folder_id = UUID(folder_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Folder not found",
        ) from None

    folder = session.get(ProjectFolder, parsed_folder_id)
    if not folder or folder.project_id != project.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Folder not found")

    project_folders = list(
        session.exec(select(ProjectFolder).where(ProjectFolder.project_id == project.id)).all()
    )
    descendant_folder_ids: set[UUID] = {folder.id}
    changed = True
    while changed:
        changed = False
        for project_folder in project_folders:
            if (
                project_folder.parent_id in descendant_folder_ids
                and project_folder.id not in descendant_folder_ids
            ):
                descendant_folder_ids.add(project_folder.id)
                changed = True

    resources_to_delete = list(
        session.exec(
            select(ProjectResource).where(
                ProjectResource.project_id == project.id,
                ProjectResource.folder_id.in_(descendant_folder_ids),
            )
        ).all()
    )
    resource_ids = [resource.id for resource in resources_to_delete]
    if resource_ids:
        session.exec(delete(ResourceExport).where(ResourceExport.resource_id.in_(resource_ids)))
        session.exec(delete(ResourceRevision).where(ResourceRevision.resource_id.in_(resource_ids)))

    for resource in resources_to_delete:
        session.delete(resource)

    folders_to_delete = [
        project_folder for project_folder in project_folders if project_folder.id in descendant_folder_ids
    ]
    folder_depth_cache: dict[UUID, int] = {}

    def folder_depth(project_folder: ProjectFolder) -> int:
        cached_depth = folder_depth_cache.get(project_folder.id)
        if cached_depth is not None:
            return cached_depth

        if project_folder.parent_id not in descendant_folder_ids:
            folder_depth_cache[project_folder.id] = 0
            return 0

        parent_folder = next(
            (
                candidate_folder
                for candidate_folder in folders_to_delete
                if candidate_folder.id == project_folder.parent_id
            ),
            None,
        )
        depth = 1 + (folder_depth(parent_folder) if parent_folder else 0)
        folder_depth_cache[project_folder.id] = depth
        return depth

    for folder_to_delete in sorted(folders_to_delete, key=folder_depth, reverse=True):
        session.delete(folder_to_delete)

    session.commit()
    publish_project_event(
        session=session,
        project_id=project.id,
        event="project.updated",
        actor_id=user_id,
    )
