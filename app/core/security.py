from datetime import UTC, datetime, timedelta
from secrets import compare_digest
from uuid import UUID

import jwt

from app.core.config import settings

ALGORITHM = "HS256"


def verify_frontend_auth_token(auth_token: str) -> bool:
    return compare_digest(auth_token, settings.FRONTEND_AUTH_TOKEN)


def create_access_token(subject: UUID, expires_delta: timedelta) -> str:
    expire = datetime.now(UTC) + expires_delta
    to_encode = {"exp": expire, "sub": str(subject)}
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict[str, object]:
    return jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
