from sqlalchemy.pool import NullPool
from sqlmodel import create_engine

from app.core.config import settings

engine = create_engine(
    str(settings.SQLALCHEMY_DATABASE_URI),
    connect_args={"prepare_threshold": None},
    pool_pre_ping=True,
    poolclass=NullPool,
)
