from databases import Database
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Identity,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    create_engine,
    func,
)
from sqlalchemy.dialects.postgresql import UUID

from src.config import settings
from src.constants import DB_NAMING_CONVENTION

DATABASE_URL_PAYMENT = settings.DATABASE_URL_PAYMENT

engine = create_engine(DATABASE_URL_PAYMENT)
metadata = MetaData(naming_convention=DB_NAMING_CONVENTION)

database = Database(DATABASE_URL_PAYMENT, force_rollback=settings.ENVIRONMENT.is_testing)


user_credit = Table(
    "user_credit",
    metadata,
    Column("uuid", Integer, Identity(), primary_key=True),
    Column("username", String, nullable=False),
    Column("credit", Integer, nullable=False),
)

metadata.create_all(engine)


