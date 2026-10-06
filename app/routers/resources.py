from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime

from app.database import get_db
from app import schemas
from app.models import ResourcePool, HoldStatus
from app.services import resource_holds

router = APIRouter(prefix="/api/resources", tags=["多资源暂占"])


@router.post("/pools/ensure", response_model=List[schemas.ResourcePoolSchema])
def ensure_pools(db: Session = Depends(get_db)):
    """根据现有展厅和主题幂等补齐资源台账（服务初始化时也会自动执行）"""
    resource_holds.ensure_base_pools(db)
    return db.query(ResourcePool).order_by(ResourcePool.id).all()


@router.get("/pools", response_model=List[schemas.ResourcePoolSchema])
def list_pools(
    resource_type: Optional[str] = Query(None, description="资源类型筛选"),
    db: Session = Depends(get_db)
):
    """资源台账列表"""
    query = db.query(ResourcePool)
    if resource_type:
        query = query.filter(ResourcePool.resource_type == resource_type)
    return query.order_by(ResourcePool.id).all()


@router.put("/pools/{pool_id}", response_model=schemas.ResourcePoolSchema)
def update_pool(pool_id: int, pool_in: schemas.ResourcePoolUpdate,
                db: Session = Depends(get_db)):
    """调整资源容量或启停状态（容量不得小于当前占用量）"""
    pool = db.query(ResourcePool).filter(ResourcePool.id == pool_id).first()
    if not pool:
        raise HTTPException(status_code=404, detail="资源池不存在")

    if pool_in.name is not None:
        pool.name = pool_in.name
    if pool_in.is_active is not None:
        pool.is_active = pool_in.is_active
    if pool_in.capacity is not None:
        if pool_in.capacity < 1:
            raise HTTPException(status_code=400, detail="容量必须大于0")
        active = db.query(resource_holds.ResourceHold).filter(
            resource_holds.ResourceHold.resource_pool_id == pool_id,
            resource_holds.ResourceHold.status.in_(resource_holds.ACTIVE_STATUSES),
        ).all()
        max_concurrent = _max_concurrent_quantity(active)
        if pool_in.capacity < max_concurrent:
            raise HTTPException(
                status_code=409,
                detail=f"当前时间段最大并发占用 {max_concurrent}，容量不能低于该值"
            )
        pool.capacity = pool_in.capacity
    db.commit()
    db.refresh(pool)
    return pool


def _max_concurrent_quantity(holds) -> int:
    if not holds:
        return 0
    events = []
    for h in holds:
        events.append((h.start_time, h.quantity))
        events.append((h.end_time, -h.quantity))
    # 同一时刻先结算结束（delta 为负）再计入开始，首尾相接不视为重叠
    events.sort(key=lambda x: (x[0], x[1]))
    current = 0
    peak = 0
    for _, delta in events:
        current += delta
        peak = max(peak, current)
    return peak


@router.get("/availability", response_model=List[schemas.ResourceAvailability])
def get_availability(
    start_time: datetime = Query(..., description="查询时段开始"),
    end_time: datetime = Query(..., description="查询时段结束"),
    db: Session = Depends(get_db)
):
    """查询某时间段各资源池的容量、已占用和可余量（含占用方明细）"""
    if end_time <= start_time:
        raise HTTPException(status_code=400, detail="结束时间必须晚于开始时间")
    return resource_holds.pool_availability(db, start_time, end_time)


@router.post("/preview", response_model=schemas.ResourcePreviewResult)
def preview_resources(preview_in: schemas.ResourcePreviewRequest,
                      db: Session = Depends(get_db)):
    """草稿/变更预审前的资源可用性预览（只检查，不暂占、不落库）"""
    try:
        requirements = resource_holds.build_session_requirements(
            db, preview_in.venue_id, preview_in.theme_id,
            device_quantity=preview_in.device_sets_needed
        )
        _, conflicts = resource_holds.check_availability(
            db, requirements, preview_in.start_time, preview_in.end_time
        )
        db.rollback()
    except resource_holds.ResourceConflict as exc:
        db.rollback()
        return schemas.ResourcePreviewResult(
            available=False,
            conflicts=exc.conflicts,
            summary=exc.message
        )

    if conflicts:
        summary = "；".join(c["message"] for c in conflicts)
        return schemas.ResourcePreviewResult(
            available=False, conflicts=conflicts, summary=summary
        )
    return schemas.ResourcePreviewResult(
        available=True, summary="所需资源在该时间段均可锁定"
    )


@router.get("/holds", response_model=List[schemas.ResourceHoldSchema])
def list_holds(
    status_filter: Optional[HoldStatus] = Query(None, alias="status"),
    session_id: Optional[int] = Query(None),
    change_request_id: Optional[int] = Query(None),
    db: Session = Depends(get_db)
):
    """查询暂占单（默认只返回活跃暂占）"""
    query = db.query(resource_holds.ResourceHold)
    if status_filter:
        query = query.filter(resource_holds.ResourceHold.status == status_filter)
    else:
        query = query.filter(
            resource_holds.ResourceHold.status.in_(resource_holds.ACTIVE_STATUSES)
        )
    if session_id is not None:
        query = query.filter(resource_holds.ResourceHold.session_id == session_id)
    if change_request_id is not None:
        query = query.filter(resource_holds.ResourceHold.change_request_id == change_request_id)
    return query.order_by(resource_holds.ResourceHold.start_time).all()


@router.post("/sweep", response_model=schemas.SweepResult)
def sweep_holds(db: Session = Depends(get_db)):
    """手动触发过期暂占清理（服务重启和后台任务也会自动执行）"""
    result = resource_holds.sweep_expired_holds(db)
    return schemas.SweepResult(**result)
