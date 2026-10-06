from sqlalchemy import create_engine, event
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

from app.config import settings

engine = create_engine(
    settings.DATABASE_URL, connect_args={"check_same_thread": False}
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_connection, connection_record):
    # 忙等待而不是立刻抛 database is locked，配合 BEGIN IMMEDIATE 做跨进程串行化
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA busy_timeout=10000")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()
    # 关闭 pysqlite 隐式事务，由 SQLAlchemy 显式发出 BEGIN IMMEDIATE
    dbapi_connection.isolation_level = None


@event.listens_for(engine, "begin")
def _begin_immediate(connection):
    # 每个事务一开始就拿写锁，杜绝“两个事务同时读到旧容量后双写”的超卖窗口
    connection.exec_driver_sql("BEGIN IMMEDIATE")


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
