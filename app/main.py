from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import inspect, text

from app.config import settings
from app.database import engine, Base, SessionLocal
from app import crud
from app.routers import staff, themes, venues, schools, sessions, reviews, warnings, statistics, changes, ranking, resources

Base.metadata.create_all(bind=engine)


def _run_lightweight_migrations():
    """SQLite 轻量迁移：为已有数据库补充新增列（新增表由 create_all 处理）"""
    additions = {
        "sessions": [
            "device_sets_needed INTEGER DEFAULT 0",
            "teaching_aids_needed INTEGER DEFAULT 0",
        ],
        "change_requests": [
            "old_device_sets_needed INTEGER",
            "new_device_sets_needed INTEGER",
            "old_teaching_aids_needed INTEGER",
            "new_teaching_aids_needed INTEGER",
        ],
    }
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table, columns in additions.items():
            if table not in existing_tables:
                continue
            existing_columns = {c["name"] for c in inspect(conn).get_columns(table)}
            for column_def in columns:
                column_name = column_def.split()[0]
                if column_name not in existing_columns:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column_def}"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    _run_lightweight_migrations()
    # 服务重启后清理超时未确认的暂占，释放被占用的资源容量
    db = SessionLocal()
    try:
        crud.cleanup_expired_holds(db)
    finally:
        db.close()
    yield


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="遗产日活动排班管理系统 - 讲解员和讲师排班、场次管理、评价统计、变更联动重排、多资源统一暂占",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(staff.router)
app.include_router(themes.router)
app.include_router(venues.router)
app.include_router(schools.router)
app.include_router(sessions.router)
app.include_router(reviews.router)
app.include_router(warnings.router)
app.include_router(statistics.router)
app.include_router(changes.router)
app.include_router(ranking.router)
app.include_router(resources.router)
app.include_router(resources.holds_router)


@app.get("/", tags=["系统"])
def root():
    return {
        "name": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "docs": "/docs",
        "status": "running"
    }


@app.get("/health", tags=["系统"])
def health_check():
    return {"status": "healthy"}
