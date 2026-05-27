from datetime import UTC, datetime, timedelta
from hashlib import sha256
from secrets import compare_digest, token_urlsafe
from uuid import UUID

import bcrypt
import jwt
import requests
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

from app.core.config import settings
from app.models import UserCreate

ALGORITHM = "HS256"


def _password_bytes(password: str) -> bytes:
    return sha256(password.encode("utf-8")).hexdigest().encode("ascii")


def get_password_hash(password: str) -> str:
    return bcrypt.hashpw(_password_bytes(password), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(_password_bytes(plain_password), password_hash.encode("utf-8"))
    except ValueError:
        return False


def create_password_reset_token() -> str:
    return token_urlsafe(32)


def get_password_reset_token_hash(token: str) -> str:
    return sha256(token.encode("utf-8")).hexdigest()


def verify_frontend_auth_token(auth_token: str) -> bool:
    return compare_digest(auth_token, settings.FRONTEND_AUTH_TOKEN)


def create_access_token(subject: UUID, expires_delta: timedelta) -> str:
    expire = datetime.now(UTC) + expires_delta
    to_encode = {"exp": expire, "sub": str(subject)}
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict[str, object]:
    return jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])


def verify_google_identity_token(credential: str) -> UserCreate | None:
    if not settings.GOOGLE_CLIENT_ID:
        return None

    try:
        payload = id_token.verify_oauth2_token(
            credential,
            google_requests.Request(),
            settings.GOOGLE_CLIENT_ID,
        )
    except ValueError:
        return None

    if payload.get("iss") not in {"accounts.google.com", "https://accounts.google.com"}:
        return None

    if not payload.get("email") or payload.get("email_verified") is not True:
        return None

    return UserCreate(
        email=str(payload["email"]),
        display_name=payload.get("name"),
        avatar_url=payload.get("picture"),
        is_admin=False,
    )


def google_token_matches_client(token_payload: dict[str, object]) -> bool:
    client_id_values = {
        token_payload.get("aud"),
        token_payload.get("audience"),
        token_payload.get("issued_to"),
        token_payload.get("azp"),
    }
    return settings.GOOGLE_CLIENT_ID in client_id_values


def google_email_is_verified(payload: dict[str, object]) -> bool:
    email_verified = payload.get("email_verified", payload.get("verified_email"))
    return email_verified in {True, "true", "True", "1"}


def verify_google_access_token(access_token: str) -> UserCreate | None:
    if not settings.GOOGLE_CLIENT_ID:
        return None

    try:
        token_response = requests.get(
            "https://oauth2.googleapis.com/tokeninfo",
            params={"access_token": access_token},
            timeout=10,
        )
        token_response.raise_for_status()
        token_payload = token_response.json()
    except requests.RequestException:
        return None

    if not google_token_matches_client(token_payload):
        return None

    try:
        userinfo_response = requests.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        userinfo_response.raise_for_status()
        userinfo = userinfo_response.json()
    except requests.RequestException:
        userinfo = {}

    identity_payload = {**token_payload, **userinfo}

    if not google_email_is_verified(identity_payload):
        return None

    if not identity_payload.get("email"):
        return None

    return UserCreate(
        email=str(identity_payload["email"]),
        display_name=identity_payload.get("name"),
        avatar_url=identity_payload.get("picture"),
        is_admin=False,
    )
