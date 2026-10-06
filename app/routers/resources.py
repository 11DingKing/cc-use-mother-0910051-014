from fastapi import APIRouter, Depends, HTTPException, Query, Body
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime

from app.database import get_db
from app import schemas, crud
from app.models import ResourceType, HoldStatus

router = APIRouter(prefix="/api/resources", tags=["资源管理"])
holds_router = APIRouter(prefix="/api/resource-holds", tags=["资源暂占"])


def _convert_hold_to_schema(hold):
    items = []
    for item in hold.items:
        items.append(schemas.ResourceHoldItem(
            id=item.id,
            resource_id=item.resource_id,
            resource_name=item.resource.name if item.resource else "",
            resource_type=item.resource.resource_type if item.resource else ResourceType.VENUE,
            quantity=item.quantity
        ))
    return schemas.ResourceHold(
        id=hold.id,
        session_id=hold.session_id,
        change_request_id=hold.change_request_id,
        status=hold.status,
        start_time=hold.start_time,
        end_time=hold.end_time,
        expires_at=hold.expires_at,
        released_at=hold.released_at,
        release_reason=hold.release_reason,
        created_by=hold.created_by,
        items=items,
        created_at=hold.created_at
    )


@router.get("", response_model=List[schemas.Resource])
def list_resources(
    skip: int = 0,
    limit: int = 100,
    resource_type: Optional[ResourceType] = Query(None, description="资源类型"),
    is_active: Optional[bool] = Query(None, description="是否启用"),
    db: Session = Depends(get_db)
):
    """获取资源列表"""
    return crud.get_resource_list(db, skip=skip, limit=limit,
                                  resource_type=resource_type, is_active=is_active)


@router.post("", response_model=schemas.Resource)
def create_resource(resource_in: schemas.ResourceCreate, db: Session = Depends(get_db)):
    """登记资源（展厅/无线设备套装/主题教具）及总容量"""
    return crud.create_resource(db, resource_in)


@router.get("/{resource_id}", response_model=schemas.Resource)
def get_resource(resource_id: int, db: Session = Depends(get_db)):
    """获取资源详情"""
    resource = crud.get_resource(db, resource_id)
    if not resource:
        raise HTTPException(status_code=404, detail="资源不存在")
    return resource


@router.put("/{resource_id}", response_model=schemas.Resource)
def update_resource(resource_id: int, resource_in: schemas.ResourceUpdate,
                    db: Session = Depends(get_db)):
    """更新资源（调整容量、启停用）"""
    resource = crud.update_resource(db, resource_id, resource_in)
    if not resource:
        raise HTTPException(status_code=404, detail="资源不存在")
    return resource


@router.get("/{resource_id}/availability", response_model=schemas.ResourceAvailability)
def get_resource_availability(
    resource_id: int,
    start_time: datetime = Query(..., description="时段开始"),
    end_time: datetime = Query(..., description="时段结束"),
    db: Session = Depends(get_db)
):
    """查询资源在指定时段的可用量与占用明细"""
    crud.cleanup_expired_holds(db)
    availability = crud.get_resource_availability(db, resource_id, start_time, end_time)
    if not availability:
        raise HTTPException(status_code=404, detail="资源不存在")
    return availability


@holds_router.get("", response_model=List[schemas.ResourceHold])
def list_holds(
    skip: int = 0,
    limit: int = 100,
    session_id: Optional[int] = Query(None, description="场次ID"),
    change_request_id: Optional[int] = Query(None, description="变更申请ID"),
    status: Optional[HoldStatus] = Query(None, description="暂占状态"),
    db: Session = Depends(get_db)
):
    """获取资源暂占列表"""
    holds = crud.get_hold_list(db, skip=skip, limit=limit, session_id=session_id,
                               change_request_id=change_request_id, status=status)
    return [_convert_hold_to_schema(h) for h in holds]


@holds_router.post("", response_model=schemas.ResourceHold)
def acquire_hold(hold_in: schemas.ResourceHoldCreate, db: Session = Depends(get_db)):
    """统一多资源暂占：任一资源不足则全部不占，返回可解释冲突集合"""
    if hold_in.start_time >= hold_in.end_time:
        raise HTTPException(status_code=400, detail={"errors": ["暂占时间段无效"]})
    if not hold_in.items:
        raise HTTPException(status_code=400, detail={"errors": ["暂占明细不能为空"]})
    if hold_in.session_id and not crud.get_session(db, hold_in.session_id):
        raise HTTPException(status_code=404, detail="场次不存在")
    if hold_in.change_request_id and not crud.get_change_request(db, hold_in.change_request_id):
        raise HTTPException(status_code=404, detail="变更申请不存在")

    demands = [{"resource_id": item.resource_id, "quantity": item.quantity}
               for item in hold_in.items]
    hold, errors, conflicts = crud.acquire_resource_hold(
        db, hold_in.start_time, hold_in.end_time, demands,
        session_id=hold_in.session_id,
        change_request_id=hold_in.change_request_id,
        ttl_minutes=hold_in.ttl_minutes,
        created_by=hold_in.created_by)
    if errors or conflicts:
        db.rollback()
        raise HTTPException(status_code=400, detail={
            "errors": errors + [c.message for c in conflicts],
            "conflicts": [c.model_dump(mode="json") for c in conflicts]
        })

    db.commit()
    return _convert_hold_to_schema(crud.get_hold(db, hold.id))


@holds_router.get("/{hold_id}", response_model=schemas.ResourceHold)
def get_hold(hold_id: int, db: Session = Depends(get_db)):
    """获取暂占详情"""
    hold = crud.get_hold(db, hold_id)
    if not hold:
        raise HTTPException(status_code=404, detail="暂占记录不存在")
    return _convert_hold_to_schema(hold)


@holds_router.post("/{hold_id}/confirm", response_model=schemas.ResourceHold)
def confirm_hold(hold_id: int, db: Session = Depends(get_db)):
    """确认暂占（审批/排定）。重复确认幂等"""
    success, errors = crud.confirm_resource_hold(db, hold_id)
    if not success:
        raise HTTPException(status_code=400, detail={"errors": errors})
    db.commit()
    return _convert_hold_to_schema(crud.get_hold(db, hold_id))


@holds_router.post("/{hold_id}/release", response_model=schemas.ResourceHold)
def release_hold(
    hold_id: int,
    reason: Optional[str] = Body(None, embed=True, description="释放原因"),
    db: Session = Depends(get_db)
):
    """释放暂占（取消）。重复释放幂等"""
    success, errors = crud.release_resource_hold(db, hold_id, reason=reason)
    if not success:
        raise HTTPException(status_code=400, detail={"errors": errors})
    db.commit()
    return _convert_hold_to_schema(crud.get_hold(db, hold_id))


@holds_router.post("/cleanup", response_model=schemas.HoldCleanupResult)
def cleanup_expired_holds(db: Session = Depends(get_db)):
    """清理超时未确认的暂占（服务重启时也会自动执行）"""
    count = crud.cleanup_expired_holds(db)
    return schemas.HoldCleanupResult(
        expired_count=count,
        message=f"已清理 {count} 条超时暂占"
    )
