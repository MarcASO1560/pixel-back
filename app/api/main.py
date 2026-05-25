from fastapi import APIRouter

from app.api.routes import exports, folders, login, projects, resources, revisions, users

api_router = APIRouter()
api_router.include_router(login.router, prefix="/auth", tags=["auth"])
api_router.include_router(users.router, prefix="/users", tags=["users"])
api_router.include_router(projects.router, prefix="/projects", tags=["projects"])
api_router.include_router(folders.router, prefix="/folders", tags=["folders"])
api_router.include_router(resources.router, prefix="/resources", tags=["resources"])
api_router.include_router(revisions.router, prefix="/revisions", tags=["revisions"])
api_router.include_router(exports.router, prefix="/exports", tags=["exports"])
