from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from app.config import settings


class Base(DeclarativeBase):
    pass


def make_engine(url):
    kw = {'connect_args': {'check_same_thread': False, 'timeout': 30}} if url.startswith('sqlite') else {}
    engine = create_engine(url, **kw)
    if url.startswith('sqlite'):
        @event.listens_for(engine, 'connect')
        def sqlite_pragmas(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute('PRAGMA foreign_keys=ON')
            cur.execute('PRAGMA journal_mode=WAL')
            cur.execute('PRAGMA busy_timeout=30000')
            cur.close()
    return engine


engine = make_engine(settings.adaptiveai_database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db():
    with SessionLocal() as db:
        yield db
