from datetime import UTC, datetime, timedelta
from secrets import compare_digest
from uuid import UUID

import jwt

from app.core.config import settings

ALGORITHM = "HS256"


def create_access_token(subject: UUID, expires_delta: timedelta) -> str:
    expire = datetime.now(UTC) + expires_delta
    to_encode = {"exp": expire, "sub": str(subject)}
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict[str, object]:
    return jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])


def verify_frontend_api_key(api_key: str) -> bool:
    return compare_digest(api_key, settings.FRONTEND_API_KEY)
