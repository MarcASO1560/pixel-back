from fastapi import APIRouter

from app.api.routes import events, login, projects, users, workspace

api_router = APIRouter()
api_router.include_router(login.router, prefix="/auth", tags=["auth"])
api_router.include_router(users.router, prefix="/users", tags=["users"])
api_router.include_router(workspace.router, prefix="/workspace", tags=["workspace"])
api_router.include_router(projects.router, prefix="/projects", tags=["projects"])
api_router.include_router(events.router, prefix="/events", tags=["events"])
