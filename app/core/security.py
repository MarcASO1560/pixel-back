from secrets import compare_digest

from app.core.config import settings


def verify_frontend_api_key(api_key: str) -> bool:
    return compare_digest(api_key, settings.FRONTEND_API_KEY)
