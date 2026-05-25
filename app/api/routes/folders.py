from fastapi import APIRouter

router = APIRouter()


@router.get("/")
def read_folders() -> dict[str, str]:
    return {"status": "not_implemented"}
