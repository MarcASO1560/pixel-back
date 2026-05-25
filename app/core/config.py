from pydantic import AnyUrl, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_CORS_ORIGINS = (
    "http://localhost:4321",
    "http://localhost:5173",
    "http://127.0.0.1:4321",
    "http://127.0.0.1:5173",
    "https://pixelartstudio.app",
    "https://www.pixelartstudio.app",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True, extra="ignore")

    PROJECT_NAME: str = "Pixel Studio API"
    ENVIRONMENT: str = "local"
    API_V1_STR: str = "/api/v1"

    BACKEND_CORS_ORIGINS: list[AnyUrl] | str = []

    DATABASE_URL: str | None = None
    POSTGRES_SERVER: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "postgres"
    POSTGRES_DB: str = "pixel_studio"

    SECRET_KEY: str = "change-this-secret-key"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7
    FRONTEND_AUTH_TOKEN: str = "change-this-frontend-auth-token"

    @computed_field
    @property
    def SQLALCHEMY_DATABASE_URI(self) -> str:
        if self.DATABASE_URL:
            return self._normalize_database_url(self.DATABASE_URL)

        return (
            f"postgresql+psycopg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_SERVER}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    def _normalize_database_url(self, database_url: str) -> str:
        if database_url.startswith("postgres://"):
            return database_url.replace("postgres://", "postgresql+psycopg://", 1)
        if database_url.startswith("postgresql://"):
            return database_url.replace("postgresql://", "postgresql+psycopg://", 1)
        return database_url

    @property
    def all_cors_origins(self) -> list[str]:
        origins = [*DEFAULT_CORS_ORIGINS, *self._configured_cors_origins()]
        return list(dict.fromkeys(origin.rstrip("/") for origin in origins if origin))

    def _configured_cors_origins(self) -> list[str]:
        if isinstance(self.BACKEND_CORS_ORIGINS, str):
            return [origin.strip() for origin in self.BACKEND_CORS_ORIGINS.split(",") if origin]
        return [str(origin).rstrip("/") for origin in self.BACKEND_CORS_ORIGINS]


settings = Settings()
