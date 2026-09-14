"""
app/db.py
SQLAlchemy 엔진 / 세션 / Base 정의.
개발 중에는 SQLite, 배포 시 DATABASE_URL만 PostgreSQL로 바꾸면 그대로 동작한다.
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import DATABASE_URL

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI 의존성 주입용 세션 제공자."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# 개발 중 컬럼이 추가될 때마다 DB를 지우지 않아도 되도록 하는 최소 마이그레이션.
# (path, column, 타입) — 이미 있으면 건너뛴다. 운영에서는 Alembic을 쓰는 게 맞다.
_ADDED_COLUMNS = [
    ("stories", "character_sheet", "TEXT"),
    ("turns", "polished_line", "TEXT"),
]


def _migrate_sqlite():
    if not DATABASE_URL.startswith("sqlite"):
        return
    with engine.begin() as conn:
        for table, column, coltype in _ADDED_COLUMNS:
            existing = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            if existing and column not in existing:
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
                print(f"[db] {table}.{column} 컬럼을 추가했습니다.")


def init_db():
    from . import models  # noqa: F401  (테이블 등록을 위해 import 필요)
    Base.metadata.create_all(bind=engine)
    _migrate_sqlite()
