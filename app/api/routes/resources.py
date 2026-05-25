from fastapi import APIRouter

router = APIRouter()


@router.get("/")
def read_resources() -> dict[str, str]:
    return {"status": "not_implemented"}
