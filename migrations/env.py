"""Migraciones de Alembic. La base de datos se toma de DTE_DATABASE_URL."""

import os

from alembic import context
from sqlalchemy import create_engine

from app import models  # noqa: F401 - registra las tablas
from app.db import Base

URL = os.environ.get('DTE_DATABASE_URL', 'sqlite:///./dte.db')


def run_migrations_offline() -> None:
    context.configure(url=URL, target_metadata=Base.metadata, literal_binds=True,
                      render_as_batch=URL.startswith('sqlite'))
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(URL)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata,
                          render_as_batch=URL.startswith('sqlite'))
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
