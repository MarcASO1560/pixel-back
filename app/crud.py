from uuid import UUID

from fastapi import HTTPException, status
from sqlmodel import Session, select

from app.core.config import settings
from app.models import (
    Project,
    ProjectCreate,
    ProjectFolder,
    ProjectResource,
    ProjectTree,
    User,
    UserCreate,
)


def get_user_by_email(*, session: Session, email: str) -> User | None:
    statement = select(User).where(User.email == email)
    return session.exec(statement).first()


def create_user(*, session: Session, user_create: UserCreate) -> User:
    existing_user = get_user_by_email(session=session, email=user_create.email)
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A user with this email already exists",
        )

    user = User(
        email=user_create.email,
        display_name=user_create.display_name,
        is_admin=user_create.is_admin,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def get_or_create_user_from_api_key(*, session: Session, user_create: UserCreate) -> User:
    user = get_user_by_email(session=session, email=user_create.email)
    if user:
        user.display_name = user_create.display_name
        user.is_admin = user_create.is_admin
        session.add(user)
        session.commit()
        session.refresh(user)
        return user

    return create_user(session=session, user_create=user_create)


def get_or_create_default_user(*, session: Session) -> User:
    user_create = UserCreate(
        email=settings.DEFAULT_USER_EMAIL,
        display_name=settings.DEFAULT_USER_DISPLAY_NAME,
        is_admin=settings.DEFAULT_USER_IS_ADMIN,
    )
    return get_or_create_user_from_api_key(session=session, user_create=user_create)


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
