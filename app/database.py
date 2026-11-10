"""SQLAlchemy engine and schema helpers; importing this module opens no connection."""

import os

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base class for the application's relational models."""


def make_engine(database_url: str, *, echo: bool = False) -> Engine:
    """Create an engine with secure MySQL SSL and SQLite foreign keys."""
    connect_args = {}

    if database_url.startswith(("mysql://", "mysql+pymysql://")):
        ssl_ca = os.environ.get("MYSQL_SSL_CA")

        if ssl_ca:
            connect_args = {
                "ssl_ca": ssl_ca,
                "ssl_verify_cert": True,
                "ssl_verify_identity": True,
            }

    engine = create_engine(
        database_url,
        echo=echo,
        pool_pre_ping=True,
        connect_args=connect_args,
    )

    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def enable_sqlite_foreign_keys(connection, _record):
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def create_schema(engine: Engine) -> None:
    """Create missing tables from the registered model metadata."""
    from . import models  # noqa: F401

    Base.metadata.create_all(engine)