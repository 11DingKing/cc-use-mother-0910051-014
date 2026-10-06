from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.database import engine, Base, SessionLocal
from app.migrations import run_migrations
from app.routers import staff, themes, venues, schools, sessions, reviews, warnings, statistics, changes, ranking, resources
from app.services import resource_holds
from app.services.hold_maintenance import start_hold_maintenance

Base.metadata.create_all(bind=engine)
run_migrations()

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="遗产日活动排班管理系统 - 讲解员和讲师排班、场次管理、评价统计、变更联动重排、多资源统一暂占"
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


@app.on_event("startup")
def _startup_resource_maintenance():
    # 幂等补齐资源台账（展厅池、主题教具池、全局设备池）
    db = SessionLocal()
    try:
        resource_holds.ensure_base_pools(db)
    finally:
        db.close()
    # 服务重启后立即清理过期暂占，并启动后台定时清扫
    start_hold_maintenance()


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
