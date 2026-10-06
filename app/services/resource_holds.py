"""统一多资源暂占服务。

一次沉浸式场次同时占用三类资源：展厅、无线设备套装（全局共享池）、
特定主题教具（按主题的容量池）。

设计要点：
- 原子性：同一暂占组合（group_id）先在单个写事务内全量校验所有资源，
  全部满足才整体写入；任一资源不足则回滚，绝不留下部分占用（无泄漏）。
- 不超卖：进程内可重入锁 + SQLite BEGIN IMMEDIATE 串行化所有写事务，
  活跃暂占（暂占中/已确认）按时间段叠加计量，并发场次也无法突破容量。
- 可解释：资源不足时返回每个不足资源的容量、已占用量、可余量，以及
  占用方（场次/变更单、时间段、数量）清单。
- 生命周期：草稿暂占与变更预审暂占带 TTL；审批确认、执行换组、
  拒绝/取消释放、超时与服务重启后清扫各有明确入口。
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    ResourcePool, ResourceHold, ResourceType, HoldStatus, HoldPurpose,
    Session as SessionModel, Venue, Theme,
)

# 所有暂占写操作在进程内串行；BEGIN IMMEDIATE 负责跨进程/跨线程的数据库层串行
_hold_lock = threading.RLock()

# 业务上仍在占用容量的暂占状态
ACTIVE_STATUSES = (HoldStatus.HELD, HoldStatus.CONFIRMED)


class ResourceConflict(Exception):
    """资源不足或参数非法：conflicts 为可解释的冲突集合。"""

    def __init__(self, conflicts: List[Dict[str, Any]], message: str = "资源暂占失败"):
        self.conflicts = conflicts
        self.message = message
        super().__init__(message)


@dataclass
class HoldRequirement:
    resource_type: ResourceType
    quantity: int = 1
    ref_id: Optional[int] = None          # 展厅ID / 主题ID；设备池为 None
    resource_name: Optional[str] = None

    def key(self) -> Tuple[ResourceType, Optional[int]]:
        return (self.resource_type, self.ref_id)


def _peak_quantity_within(window_start: datetime, window_end: datetime,
                          holds: List[ResourceHold]) -> int:
    """计算各暂占与请求窗口交集上的数量加权最大并发。

    区间为半开 [start, end)：首尾相接的两个暂占（10:00 结束 / 10:00 开始）
    不算重叠，因此同一时刻先结算结束再计入开始。
    """
    events = []
    for hold in holds:
        seg_start = max(window_start, hold.start_time)
        seg_end = min(window_end, hold.end_time)
        if seg_start < seg_end:
            events.append((seg_start, hold.quantity))
            events.append((seg_end, -hold.quantity))
    events.sort(key=lambda x: (x[0], x[1]))
    current = 0
    peak = 0
    for _, delta in events:
        current += delta
        peak = max(peak, current)
    return peak


def get_or_create_pool(db: Session, req: HoldRequirement) -> ResourcePool:
    ref_condition = (
        ResourcePool.ref_id.is_(None)
        if req.ref_id is None
        else ResourcePool.ref_id == req.ref_id
    )
    pool = db.query(ResourcePool).filter(
        ResourcePool.resource_type == req.resource_type,
        ref_condition
    ).first()
    if pool:
        return pool

    name = req.resource_name
    if name is None:
        if req.resource_type == ResourceType.DEVICE:
            name = "无线设备套装（共享池）"
        else:
            name = f"{req.resource_type.value}#{req.ref_id}"

    pool = ResourcePool(
        resource_type=req.resource_type,
        ref_id=req.ref_id,
        name=name,
        capacity=(settings.DEVICE_POOL_CAPACITY
                  if req.resource_type == ResourceType.DEVICE else 1)
    )
    db.add(pool)
    db.flush()
    return pool


def build_session_requirements(db: Session,
                               venue_id: int,
                               theme_id: int,
                               device_quantity: int = 1,
                               device_name: Optional[str] = None) -> List[HoldRequirement]:
    """根据展厅与主题构造三类资源需求；同时补齐台账名称并校验实体存在。"""
    venue = db.query(Venue).filter(Venue.id == venue_id).first()
    theme = db.query(Theme).filter(Theme.id == theme_id).first()
    if not venue:
        raise ResourceConflict([{
            "resource_type": ResourceType.VENUE.value,
            "resource_name": f"展厅#{venue_id}",
            "message": f"展厅 {venue_id} 不存在",
        }])
    if not theme:
        raise ResourceConflict([{
            "resource_type": ResourceType.TEACHING_KIT.value,
            "resource_name": f"主题教具#{theme_id}",
            "message": f"主题 {theme_id} 不存在",
        }])

    return [
        HoldRequirement(ResourceType.VENUE, 1, venue.id, venue.name),
        HoldRequirement(ResourceType.DEVICE, max(1, device_quantity), None,
                        device_name or "无线设备套装（共享池）"),
        HoldRequirement(ResourceType.TEACHING_KIT, 1, theme.id, f"{theme.name}教具套装"),
    ]


def requirements_for_session(db: Session, session: SessionModel,
                             device_quantity: int = 1) -> List[HoldRequirement]:
    return build_session_requirements(
        db, session.venue_id, session.theme_id, device_quantity
    )


def _is_effectively_active(row: ResourceHold, now: datetime) -> bool:
    """容量计量口径：HELD 超过 TTL 即视为失效（即使清扫线程尚未跑到），
    避免过期暂占在两次清扫之间继续占压容量。"""
    if row.status not in ACTIVE_STATUSES:
        return False
    if row.status == HoldStatus.HELD and row.expires_at <= now:
        return False
    return True


def _active_holds_for_pool(db: Session, pool_id: int,
                           start: datetime, end: datetime,
                           exclude_group_ids: Optional[set] = None,
                           exclude_session_id: Optional[int] = None,
                           now: Optional[datetime] = None) -> List[ResourceHold]:
    now = now or datetime.now()
    rows = db.query(ResourceHold).filter(
        ResourceHold.resource_pool_id == pool_id,
        ResourceHold.status.in_(ACTIVE_STATUSES),
        ResourceHold.start_time < end,
        ResourceHold.end_time > start,
    ).all()
    result = []
    for row in rows:
        if not _is_effectively_active(row, now):
            continue
        if exclude_group_ids and row.group_id in exclude_group_ids:
            continue
        if exclude_session_id is not None and row.session_id == exclude_session_id:
            continue
        result.append(row)
    return result


def _holder_info(hold: ResourceHold) -> Dict[str, Any]:
    title = hold.session.title if hold.session else None
    return {
        "group_id": hold.group_id,
        "session_id": hold.session_id,
        "session_title": title,
        "change_request_id": hold.change_request_id,
        "purpose": hold.purpose.value,
        "status": hold.status.value,
        "quantity": hold.quantity,
        "start_time": hold.start_time.isoformat(),
        "end_time": hold.end_time.isoformat(),
    }


def check_availability(db: Session, requirements: List[HoldRequirement],
                       start_time: datetime, end_time: datetime,
                       exclude_group_ids: Optional[set] = None,
                       exclude_session_id: Optional[int] = None
                       ) -> Tuple[List[ResourcePool], List[Dict[str, Any]]]:
    """只校验不写入。返回（涉及的资源池列表，冲突列表）；冲突为空表示全部可获取。"""
    conflicts: List[Dict[str, Any]] = []
    if end_time <= start_time:
        raise ResourceConflict([{
            "resource_type": None,
            "resource_name": None,
            "message": f"结束时间 {end_time} 必须晚于开始时间 {start_time}",
        }])

    # 合并同池需求（例如多个需求指向同一展厅时数量相加）
    merged: Dict[Tuple[ResourceType, Optional[int]], HoldRequirement] = {}
    for req in requirements:
        if req.quantity <= 0:
            raise ResourceConflict([{
                "resource_type": req.resource_type.value,
                "resource_name": req.resource_name,
                "message": "资源需求数量必须大于 0",
            }])
        if req.key() in merged:
            merged[req.key()].quantity += req.quantity
        else:
            merged[req.key()] = req

    pools: List[ResourcePool] = []
    for req in merged.values():
        pool = get_or_create_pool(db, req)
        pools.append(pool)
        if not pool.is_active:
            conflicts.append({
                "resource_type": req.resource_type.value,
                "resource_name": pool.name,
                "required": req.quantity,
                "capacity": pool.capacity,
                "available": 0,
                "held_by_others": 0,
                "start_time": start_time.isoformat(),
                "end_time": end_time.isoformat(),
                "holders": [],
                "message": f"资源「{pool.name}」已停用，无法暂占",
            })
            continue

        others = _active_holds_for_pool(
            db, pool.id, start_time, end_time,
            exclude_group_ids=exclude_group_ids,
            exclude_session_id=exclude_session_id,
        )
        # 容量口径取窗口内最大并发，而不是对所有重叠暂占求和：
        # 两个时间不相交（首尾相接）的暂占可以复用同一份容量。
        held_by_others = _peak_quantity_within(start_time, end_time, others)
        available = pool.capacity - held_by_others
        if req.quantity > available:
            conflicts.append({
                "resource_type": req.resource_type.value,
                "resource_name": pool.name,
                "resource_pool_id": pool.id,
                "required": req.quantity,
                "capacity": pool.capacity,
                "available": max(0, available),
                "held_by_others": held_by_others,
                "start_time": start_time.isoformat(),
                "end_time": end_time.isoformat(),
                "holders": [_holder_info(h) for h in others],
                "message": (
                    f"资源「{pool.name}」在 {start_time:%Y-%m-%d %H:%M} ~ "
                    f"{end_time:%Y-%m-%d %H:%M} 不足：需要 {req.quantity}，"
                    f"容量 {pool.capacity}，时段内最大并发占用 {held_by_others}，仅剩 {max(0, available)}"
                ),
            })
    return pools, conflicts


def acquire_holds(db: Session,
                  requirements: List[HoldRequirement],
                  start_time: datetime,
                  end_time: datetime,
                  purpose: HoldPurpose,
                  ttl_seconds: int,
                  session_id: Optional[int] = None,
                  change_request_id: Optional[int] = None,
                  exclude_group_ids: Optional[set] = None,
                  exclude_session_id: Optional[int] = None,
                  ) -> str:
    """原子获取整组资源。成功返回 group_id；任一资源不足整体回滚并抛 ResourceConflict。"""
    now = datetime.now()
    with _hold_lock:
        try:
            pools, conflicts = check_availability(
                db, requirements, start_time, end_time,
                exclude_group_ids=exclude_group_ids,
                exclude_session_id=exclude_session_id,
            )
            if conflicts:
                db.rollback()
                raise ResourceConflict(conflicts)

            group_id = str(uuid.uuid4())
            expires_at = datetime.fromtimestamp(now.timestamp() + ttl_seconds)
            pool_by_key = {p.resource_type: p for p in pools}
            for req in requirements:
                pool = pool_by_key[req.resource_type]
                db.add(ResourceHold(
                    group_id=group_id,
                    resource_pool_id=pool.id,
                    resource_type=req.resource_type,
                    resource_name=pool.name,
                    session_id=session_id,
                    change_request_id=change_request_id,
                    purpose=purpose,
                    status=HoldStatus.HELD,
                    quantity=req.quantity,
                    start_time=start_time,
                    end_time=end_time,
                    expires_at=expires_at,
                ))
            db.commit()
            return group_id
        except ResourceConflict:
            raise
        except Exception:
            db.rollback()
            raise


def get_active_hold_group(db: Session, *, session_id: Optional[int] = None,
                          change_request_id: Optional[int] = None,
                          purpose: Optional[HoldPurpose] = None) -> Optional[List[ResourceHold]]:
    """查找某场次/变更单当前仍有效的整组暂占（HELD 或 CONFIRMED）。"""
    query = db.query(ResourceHold).filter(ResourceHold.status.in_(ACTIVE_STATUSES))
    if session_id is not None:
        query = query.filter(ResourceHold.session_id == session_id)
    if change_request_id is not None:
        query = query.filter(ResourceHold.change_request_id == change_request_id)
    if purpose is not None:
        query = query.filter(ResourceHold.purpose == purpose)
    rows = query.order_by(ResourceHold.id).all()
    if not rows:
        return None
    return rows


def _load_group(db: Session, group_id: str) -> List[ResourceHold]:
    return db.query(ResourceHold).filter(ResourceHold.group_id == group_id).all()


def release_holds(db: Session, group_id: str, reason: str = "主动释放") -> int:
    """释放整组暂占。重复释放是幂等的，返回本次变更的行数。"""
    with _hold_lock:
        rows = _load_group(db, group_id)
        changed = 0
        for row in rows:
            if row.status in ACTIVE_STATUSES:
                row.status = HoldStatus.RELEASED
                row.released_at = datetime.now()
                row.released_reason = reason
                changed += 1
        db.commit()
        return changed


def release_owner_holds(db: Session, *, session_id: Optional[int] = None,
                        change_request_id: Optional[int] = None,
                        keep_group_id: Optional[str] = None,
                        reason: str = "主动释放") -> int:
    """按属主释放暂占（可保留某一组，用于换组场景）。"""
    query = db.query(ResourceHold).filter(ResourceHold.status.in_(ACTIVE_STATUSES))
    if session_id is not None:
        query = query.filter(ResourceHold.session_id == session_id)
    if change_request_id is not None:
        query = query.filter(ResourceHold.change_request_id == change_request_id)
    rows = query.all()
    with _hold_lock:
        changed = 0
        for row in rows:
            if keep_group_id and row.group_id == keep_group_id:
                continue
            row.status = HoldStatus.RELEASED
            row.released_at = datetime.now()
            row.released_reason = reason
            changed += 1
        db.commit()
        return changed


def confirm_holds(db: Session, group_id: str) -> bool:
    """审批通过：把暂占中确认下来。重复确认幂等；组已失效返回 False。"""
    with _hold_lock:
        rows = _load_group(db, group_id)
        active = [r for r in rows if r.status in ACTIVE_STATUSES]
        if not active:
            return False
        now = datetime.now()
        held = [r for r in active if r.status == HoldStatus.HELD]
        if not held:
            # 全部已经是 CONFIRMED —— 重复审批/确认，幂等成功（不动调用方事务）
            return True
        for row in held:
            if row.expires_at <= now:
                raise ResourceConflict([], message="暂占已超时失效，请重新提交")
        for row in held:
            row.status = HoldStatus.CONFIRMED
            row.confirmed_at = now
            # 确认后有效期跟随场次结束时间，结束后由清扫任务释放
            row.expires_at = row.end_time
        db.commit()
        return True


def swap_holds(db: Session,
               new_requirements: List[HoldRequirement],
               new_start: datetime, new_end: datetime,
               old_group_id: str,
               ttl_seconds: int,
               session_id: int,
               change_request_id: Optional[int] = None,
               also_exclude_group_ids: Optional[set] = None,
               confirm: bool = True) -> str:
    """改期/变更执行：先在同一把锁内验证并获取新组合，成功后才释放旧组合。

    新组合包含旧组的排除项，避免旧暂占挡住自己的新组合。
    confirm=False 时新组保持 HELD（草稿 TTL 语义），用于直接编辑草稿场次。
    """
    exclude = {old_group_id}
    if also_exclude_group_ids:
        exclude.update(also_exclude_group_ids)
    with _hold_lock:
        old_rows = _load_group(db, old_group_id)
        if not any(r.status in ACTIVE_STATUSES for r in old_rows):
            raise ResourceConflict([], message="原暂占组合不存在或已释放，无法改期")

        new_group_id = acquire_holds(
            db,
            new_requirements,
            new_start, new_end,
            HoldPurpose.DRAFT,
            ttl_seconds,
            session_id=session_id,
            change_request_id=change_request_id,
            exclude_group_ids=exclude,
            exclude_session_id=session_id,
        )
        try:
            # 新组合已落库，再安全释放旧组合
            for row in old_rows:
                if row.status in ACTIVE_STATUSES:
                    row.status = HoldStatus.RELEASED
                    row.released_at = datetime.now()
                    row.released_reason = "改期换组释放"
            # 新组：已排定场次直接确认（有效期跟随新时段）；草稿保持 HELD + TTL
            new_rows = _load_group(db, new_group_id)
            for row in new_rows:
                row.purpose = HoldPurpose.DRAFT
                row.change_request_id = None
                if confirm:
                    row.status = HoldStatus.CONFIRMED
                    row.confirmed_at = datetime.now()
                    row.expires_at = row.end_time
            db.commit()
            return new_group_id
        except Exception:
            db.rollback()
            # 回滚新组合，保持旧组合仍然有效
            release_holds(db, new_group_id, reason="换组失败回滚")
            raise


def downgrade_holds_to_held(db: Session, group_id: str, ttl_seconds: int) -> None:
    """场次从已排定退回草稿时：暂占恢复 HELD 并重新计算草稿 TTL。"""
    now = datetime.now()
    with _hold_lock:
        rows = _load_group(db, group_id)
        for row in rows:
            if row.status != HoldStatus.CONFIRMED:
                continue
            row.status = HoldStatus.HELD
            row.confirmed_at = None
            row.expires_at = datetime.fromtimestamp(now.timestamp() + ttl_seconds)
        db.commit()


def promote_change_holds(db: Session, session_id: int,
                         change_group_id: str,
                         new_expires: Optional[datetime] = None) -> None:
    """变更执行：预审暂占组就是已经验证成功的“新组合”，它本身持有容量。

    顺序严格为：确认新组仍有效 → 释放场次的旧组合 → 把新组提升为场次正式
    （CONFIRMED、归属 DRAFT 用途）。不会出现旧组已释放而新组拿不到的空窗。
    """
    with _hold_lock:
        change_rows = [r for r in _load_group(db, change_group_id)
                       if r.status in ACTIVE_STATUSES]
        if not change_rows:
            raise ResourceConflict(
                [], message="变更预审暂占已超时或被释放，请重新提交变更申请"
            )

        now = datetime.now()
        # 释放该场次除新组之外的所有活跃暂占（旧时间/旧资源组合）
        others = db.query(ResourceHold).filter(
            ResourceHold.session_id == session_id,
            ResourceHold.status.in_(ACTIVE_STATUSES),
            ResourceHold.group_id != change_group_id,
        ).all()
        for row in others:
            row.status = HoldStatus.RELEASED
            row.released_at = now
            row.released_reason = "变更执行释放旧组合"

        # 新组提升为场次正式暂占
        for row in change_rows:
            row.session_id = session_id
            row.purpose = HoldPurpose.DRAFT
            row.change_request_id = None
            row.status = HoldStatus.CONFIRMED
            row.confirmed_at = now
            row.expires_at = new_expires or row.end_time
        db.commit()


def sweep_expired_holds(db: Session, now: Optional[datetime] = None) -> Dict[str, int]:
    """过期清理：

    - 暂占中(HELD)且超过 expires_at（TTL 到期，审批/确认未发生）→ 标记过期；
    - 任何活跃暂占的场次时间段已完全过去（结束时间 <= now）→ 释放。
    可在服务重启时和后台定时任务中安全重复调用。
    """
    now = now or datetime.now()
    with _hold_lock:
        rows = db.query(ResourceHold).filter(
            ResourceHold.status.in_(ACTIVE_STATUSES)
        ).all()
        expired_ttl = 0
        released_past = 0
        for row in rows:
            if row.status == HoldStatus.HELD and row.expires_at <= now:
                row.status = HoldStatus.EXPIRED
                row.released_at = now
                row.released_reason = "超时自动释放"
                expired_ttl += 1
            elif row.end_time <= now:
                row.status = HoldStatus.RELEASED
                row.released_at = now
                row.released_reason = "场次结束自动释放"
                released_past += 1
        db.commit()
        return {"expired": expired_ttl, "released_past": released_past}


def ensure_base_pools(db: Session) -> None:
    """幂等补齐基础台账：全局设备池、所有展厅池、所有主题教具池。"""
    from app.models import Venue, Theme

    device_pool = get_or_create_pool(db, HoldRequirement(
        ResourceType.DEVICE, settings.DEVICE_POOL_CAPACITY, None, "无线设备套装（共享池）"
    ))
    if device_pool.capacity != settings.DEVICE_POOL_CAPACITY:
        device_pool.capacity = settings.DEVICE_POOL_CAPACITY

    for venue in db.query(Venue).all():
        pool = get_or_create_pool(db, HoldRequirement(ResourceType.VENUE, 1, venue.id, venue.name))
        pool.name = venue.name
    for theme in db.query(Theme).all():
        pool = get_or_create_pool(db, HoldRequirement(
            ResourceType.TEACHING_KIT, 1, theme.id, f"{theme.name}教具套装"
        ))
        pool.name = f"{theme.name}教具套装"
    db.commit()


def pool_availability(db: Session, start_time: datetime, end_time: datetime) -> List[Dict[str, Any]]:
    """列出给定时间段内每个资源池的占用与余量，供前端预览。"""
    pools = db.query(ResourcePool).filter(ResourcePool.is_active == True).all()
    result = []
    for pool in pools:
        holders = _active_holds_for_pool(db, pool.id, start_time, end_time)
        held = _peak_quantity_within(start_time, end_time, holders)
        result.append({
            "resource_pool_id": pool.id,
            "resource_type": pool.resource_type.value,
            "resource_name": pool.name,
            "ref_id": pool.ref_id,
            "capacity": pool.capacity,
            "held": held,
            "available": max(0, pool.capacity - held),
            "holders": [_holder_info(h) for h in holders],
        })
    return result
