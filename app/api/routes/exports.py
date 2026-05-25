from fastapi import APIRouter

router = APIRouter()


@router.get("/")
def read_exports() -> dict[str, str]:
    return {"status": "not_implemented"}
