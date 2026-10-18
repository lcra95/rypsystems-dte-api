"""Motor y sesiones de SQLAlchemy."""

from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def crear_engine(url: str) -> Engine:
    if url.startswith('sqlite'):
        engine = create_engine(url, connect_args={'check_same_thread': False})

        @event.listens_for(engine, 'connect')
        def _sqlite(conexion, _):
            cursor = conexion.cursor()
            cursor.execute('PRAGMA foreign_keys=ON')
            cursor.execute('PRAGMA busy_timeout=10000')
            cursor.close()
        return engine
    return create_engine(url, pool_pre_ping=True)


class Database:
    def __init__(self, url: str):
        self.engine = crear_engine(url)
        self.sesiones = sessionmaker(self.engine, expire_on_commit=False)

    def sesion(self) -> Iterator[Session]:
        with self.sesiones() as sesion:
            yield sesion
