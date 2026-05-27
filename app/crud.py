from datetime import datetime, timedelta
from uuid import UUID

from fastapi import HTTPException, status
from sqlmodel import Session, select

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
    ProjectCreate,
    ProjectFolder,
    ProjectFolderCreate,
    ProjectFolderUpdate,
    ProjectResource,
    ProjectTree,
    ProjectUpdate,
    User,
    UserCreate,
    UserRegistrationCreate,
)

PASSWORD_RESET_TOKEN_MINUTES = 30


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
        display_name=user_create.display_name,
        avatar_url=user_create.avatar_url,
        is_admin=user_create.is_admin,
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
        user.display_name = user_create.display_name
        user.avatar_url = user_create.avatar_url
        user.is_admin = user_create.is_admin
        user.updated_at = datetime.utcnow()
        session.add(user)
        session.commit()
        session.refresh(user)
        return user

    return create_user(session=session, user_create=user_create)


def get_or_create_user_from_api_key(*, session: Session, user_create: UserCreate) -> User:
    return upsert_user_from_identity(session=session, user_create=user_create)


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
        display_name=username,
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
        expires_at=datetime.utcnow() + timedelta(minutes=PASSWORD_RESET_TOKEN_MINUTES),
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
        or reset_token.expires_at <= datetime.utcnow()
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired reset token",
        )

    password_credential = session.get(PasswordCredential, reset_token.user_id)
    if password_credential:
        password_credential.password_hash = get_password_hash(reset_confirm.password)
        password_credential.updated_at = datetime.utcnow()
    else:
        password_credential = PasswordCredential(
            user_id=reset_token.user_id,
            password_hash=get_password_hash(reset_confirm.password),
        )

    reset_token.used_at = datetime.utcnow()
    session.add(password_credential)
    session.add(reset_token)
    session.commit()

    user = session.get(User, reset_token.user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    return user


def list_projects(*, session: Session, owner_id: UUID) -> list[Project]:
    statement = (
        select(Project)
        .where(Project.owner_id == owner_id, Project.deleted_at.is_(None))
        .order_by(Project.updated_at.desc())
    )
    return list(session.exec(statement).all())


def create_project(*, session: Session, owner_id: UUID, project_create: ProjectCreate) -> Project:
    project = Project.model_validate(project_create, update={"owner_id": owner_id})
    session.add(project)
    session.commit()
    session.refresh(project)
    return project


def update_project(
    *,
    session: Session,
    owner_id: UUID,
    project_id: str,
    project_update: ProjectUpdate,
) -> Project:
    project = get_project_or_404(session=session, owner_id=owner_id, project_id=project_id)
    project_data = project_update.model_dump(exclude_unset=True)

    for field, value in project_data.items():
        setattr(project, field, value)

    project.updated_at = datetime.utcnow()
    session.add(project)
    session.commit()
    session.refresh(project)
    return project


def get_project_or_404(*, session: Session, owner_id: UUID, project_id: str) -> Project:
    try:
        parsed_project_id = UUID(project_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        ) from None

    project = session.get(Project, parsed_project_id)
    if not project or project.owner_id != owner_id or project.deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return project


def get_project_tree(*, session: Session, owner_id: UUID, project_id: str) -> ProjectTree:
    project = get_project_or_404(session=session, owner_id=owner_id, project_id=project_id)

    folders_statement = (
        select(ProjectFolder)
        .where(ProjectFolder.project_id == project.id)
        .order_by(ProjectFolder.position, ProjectFolder.name)
    )
    resources_statement = (
        select(ProjectResource)
        .where(ProjectResource.project_id == project.id, ProjectResource.deleted_at.is_(None))
        .order_by(ProjectResource.position, ProjectResource.name)
    )

    return ProjectTree(
        folders=list(session.exec(folders_statement).all()),
        resources=list(session.exec(resources_statement).all()),
    )


def create_project_folder(
    *,
    session: Session,
    owner_id: UUID,
    project_id: str,
    folder_create: ProjectFolderCreate,
) -> ProjectFolder:
    project = get_project_or_404(session=session, owner_id=owner_id, project_id=project_id)

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
    return folder


def update_project_folder(
    *,
    session: Session,
    owner_id: UUID,
    project_id: str,
    folder_id: str,
    folder_update: ProjectFolderUpdate,
) -> ProjectFolder:
    project = get_project_or_404(session=session, owner_id=owner_id, project_id=project_id)

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
    for field, value in folder_data.items():
        setattr(folder, field, value)

    folder.updated_at = datetime.utcnow()
    session.add(folder)
    session.commit()
    session.refresh(folder)
    return folder
