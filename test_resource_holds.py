#!/usr/bin/env python3
"""多资源统一暂占功能验证脚本

覆盖：草稿暂占、全有或全无、可解释冲突集合、改期先验证后释放、
审批/执行/取消/超时的确认与释放、重复审批与并发不超卖、重启过期清理。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

from app.database import SessionLocal, Base, engine
from app import crud, schemas
from app.models import (
    StaffType, SessionType, SessionStatus, AssignmentRole,
    AudienceType, ChangeType, ChangeStatus, ResourceType, HoldStatus,
    Resource, ResourceHold
)


def test_header(title):
    print("\n" + "=" * 70)
    print(f"  {title}")
    print("=" * 70)


def make_session_in(title, theme_id, venue_id, start, end, device=0, aids=0):
    return schemas.SessionCreate(
        title=title, theme_id=theme_id, venue_id=venue_id,
        session_type=SessionType.RESEARCH,
        start_time=start, end_time=end,
        audience_type=AudienceType.SCHOOL, audience_count=30,
        guides_needed=0, needs_lecturer=False,
        device_sets_needed=device, teaching_aids_needed=aids
    )


def active_holds(db, session_id=None, change_request_id=None):
    query = db.query(ResourceHold).filter(
        ResourceHold.status.in_([HoldStatus.HELD, HoldStatus.CONFIRMED]))
    if session_id is not None:
        query = query.filter(ResourceHold.session_id == session_id)
    if change_request_id is not None:
        query = query.filter(ResourceHold.change_request_id == change_request_id)
    return query.all()


def main():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    tomorrow = datetime.now() + timedelta(days=1)

    try:
        test_header("1. 初始化：主题/场地/人员 + 三类资源登记")
        theme = crud.create_theme(db, schemas.ThemeCreate(name="青铜器文化", category="历史"))
        theme2 = crud.create_theme(db, schemas.ThemeCreate(name="茶文化", category="传统"))
        venue = crud.create_venue(db, schemas.VenueCreate(
            name="青铜馆", venue_type="展厅", capacity=50, location="1楼"))
        school = crud.create_school(db, schemas.SchoolCreate(name="实验小学"))

        hall_res = crud.create_resource(db, schemas.ResourceCreate(
            name="青铜馆展厅", resource_type=ResourceType.VENUE,
            total_quantity=1, venue_id=venue.id))
        device_res = crud.create_resource(db, schemas.ResourceCreate(
            name="无线讲解设备套装", resource_type=ResourceType.DEVICE_SET,
            total_quantity=5))
        aid_res = crud.create_resource(db, schemas.ResourceCreate(
            name="青铜器主题教具", resource_type=ResourceType.TEACHING_AID,
            total_quantity=3, theme_id=theme.id))
        print(f"✓ 资源登记: 展厅x1, 设备x{device_res.total_quantity}, 教具x{aid_res.total_quantity}")

        test_header("2. 草稿创建即暂占：场次A 9:00-11:00 设备2+教具1")
        s_a, errors, conflicts = crud.create_session_with_holds(db, make_session_in(
            "青铜研学A", theme.id, venue.id,
            tomorrow.replace(hour=9, minute=0, second=0, microsecond=0),
            tomorrow.replace(hour=11, minute=0, second=0, microsecond=0),
            device=2, aids=1))
        assert s_a and not conflicts, f"场次A创建失败: {errors}"
        holds_a = active_holds(db, session_id=s_a.id)
        assert len(holds_a) == 1 and holds_a[0].status == HoldStatus.HELD
        assert len(holds_a[0].items) == 3, "应暂占展厅+设备+教具三项"
        assert holds_a[0].expires_at is not None, "暂占中应有过期时间"
        print(f"✓ 场次A(id={s_a.id}) 暂占成功: 展厅1+设备2+教具1, 状态={holds_a[0].status.value}")

        avail = crud.get_resource_availability(
            db, device_res.id,
            tomorrow.replace(hour=10, minute=0), tomorrow.replace(hour=12, minute=0))
        assert avail.available_quantity == 3 and avail.held_quantity == 2
        print(f"✓ 设备池可用量: {avail.available_quantity}/{avail.total_quantity}")

        test_header("3. 全有或全无：任一资源不足则整体拒绝，不产生部分占用")
        s_b, errors, conflicts = crud.create_session_with_holds(db, make_session_in(
            "青铜研学B", theme.id, venue.id,
            tomorrow.replace(hour=10, minute=0, second=0, microsecond=0),
            tomorrow.replace(hour=12, minute=0, second=0, microsecond=0),
            device=4, aids=1))
        assert s_b is None and len(conflicts) >= 2, "展厅和设备都应冲突"
        types = {c.resource_type for c in conflicts}
        assert ResourceType.VENUE in types and ResourceType.DEVICE_SET in types
        device_conflict = [c for c in conflicts if c.resource_type == ResourceType.DEVICE_SET][0]
        assert device_conflict.required_quantity == 4
        assert device_conflict.available_quantity == 3
        assert len(device_conflict.holders) == 1, "应指出被场次A占用"
        print(f"✓ 冲突集合可解释，共{len(conflicts)}条:")
        for c in conflicts:
            print(f"    - {c.message} (占用方场次: {[h.session_title for h in c.holders]})")
        leaked = db.query(ResourceHold).filter(
            ResourceHold.status.in_([HoldStatus.HELD, HoldStatus.CONFIRMED])).count()
        assert leaked == 1, f"失败后不应新增任何暂占，实际有{leaked}条"
        avail = crud.get_resource_availability(
            db, device_res.id,
            tomorrow.replace(hour=10, minute=0), tomorrow.replace(hour=12, minute=0))
        assert avail.available_quantity == 3, "失败暂占不得泄漏设备容量"
        print("✓ 未产生任何部分暂占，设备容量无泄漏")

        test_header("4. 同展厅时段重叠：第二个场次因展厅不足被拒")
        s_b2, errors, conflicts = crud.create_session_with_holds(db, make_session_in(
            "青铜研学B2", theme.id, venue.id,
            tomorrow.replace(hour=10, minute=0, second=0, microsecond=0),
            tomorrow.replace(hour=12, minute=0, second=0, microsecond=0),
            device=1, aids=0))
        assert s_b2 is None and any(c.resource_type == ResourceType.VENUE for c in conflicts)
        print(f"✓ 展厅时段冲突被拦截: {conflicts[0].message}")

        test_header("5. 不重叠场次可正常暂占（场次C 14:00-16:00 设备3）")
        s_c, errors, conflicts = crud.create_session_with_holds(db, make_session_in(
            "青铜研学C", theme.id, venue.id,
            tomorrow.replace(hour=14, minute=0, second=0, microsecond=0),
            tomorrow.replace(hour=16, minute=0, second=0, microsecond=0),
            device=3, aids=2))
        assert s_c and not conflicts, f"场次C创建失败: {errors}"
        print(f"✓ 场次C(id={s_c.id}) 暂占成功")

        test_header("6. 改期：先验证新组合成功后再释放旧组合")
        # 6a. 改期到与C重叠且设备不足的时段 → 失败，旧组合保留
        _, errors, conflicts = crud.update_session(db, s_a.id, schemas.SessionUpdate(
            start_time=tomorrow.replace(hour=15, minute=0),
            end_time=tomorrow.replace(hour=17, minute=0)))
        assert conflicts, "设备不足(3被C占)应导致改期失败"
        holds_a = active_holds(db, session_id=s_a.id)
        assert len(holds_a) == 1 and holds_a[0].start_time.hour == 9, "旧组合暂占必须保留"
        db.refresh(s_a)
        assert s_a.start_time.hour == 9, "改期失败时场次时间不得变更"
        print(f"✓ 改期失败时旧组合保留: {conflicts[0].message}")

        # 6b. 改期到可行时段 18:00-20:00 → 新组合生效，旧组合释放
        s_a2, errors, conflicts = crud.update_session(db, s_a.id, schemas.SessionUpdate(
            start_time=tomorrow.replace(hour=18, minute=0),
            end_time=tomorrow.replace(hour=20, minute=0)))
        assert not conflicts, f"可行改期不应失败: {errors}"
        holds_a = active_holds(db, session_id=s_a.id)
        assert len(holds_a) == 1 and holds_a[0].start_time.hour == 18, "应只有新组合暂占"
        avail = crud.get_resource_availability(
            db, device_res.id,
            tomorrow.replace(hour=9, minute=0), tomorrow.replace(hour=11, minute=0))
        assert avail.available_quantity == 5, "旧时段设备应已全部释放"
        print("✓ 改期成功：新组合暂占生效，旧组合已释放（9-11点设备恢复为5）")

        test_header("7. 变更预审暂占 → 审批确认 → 执行转移并释放旧组合")
        # 场次C申请改期到 15:00-17:00（与自身旧组合部分重叠，设备仍要3）
        change, errors, conflicts = crud.submit_change_request(db, schemas.ChangeRequestCreate(
            session_id=s_c.id, requester="王老师", change_type=ChangeType.TIME,
            new_start_time=tomorrow.replace(hour=15, minute=0),
            new_end_time=tomorrow.replace(hour=17, minute=0),
            reason="学校调整到校时间"))
        assert change and not conflicts, f"变更预审失败: {errors}"
        hold_c = active_holds(db, change_request_id=change.id)
        assert len(hold_c) == 1 and hold_c[0].status == HoldStatus.HELD
        assert len(active_holds(db, session_id=s_c.id)) == 1, "旧组合暂占仍在"
        print(f"✓ 预审暂占新组合(15-17点)，旧组合(14-16点)仍保留")

        # 预审暂占保护：此时其他场次不能在15-17点抢走设备
        s_d, errors, conflicts = crud.create_session_with_holds(db, make_session_in(
            "青铜研学D", theme.id, venue.id,
            tomorrow.replace(hour=16, minute=30, second=0, microsecond=0),
            tomorrow.replace(hour=18, minute=0, second=0, microsecond=0),
            device=3, aids=0))
        assert s_d is None and any(c.resource_type == ResourceType.DEVICE_SET for c in conflicts), \
            "变更暂占应阻止其他场次抢走设备"
        print(f"✓ 预审暂占生效，其他场次无法抢走设备: {conflicts[0].message}")

        # 审批通过 → 确认暂占
        change, errors, conflicts = crud.perform_change_review(db, change.id, schemas.ChangeRequestReview(
            status=ChangeStatus.APPROVED, reviewer="调度员", review_comment="同意"))
        assert change and change.status == ChangeStatus.APPROVED
        hold_c = active_holds(db, change_request_id=change.id)
        assert len(hold_c) == 1 and hold_c[0].status == HoldStatus.CONFIRMED
        assert hold_c[0].expires_at is None, "确认后不再有过期时间"
        print("✓ 审批通过，暂占已确认")

        # 重复审批 → 明确拒绝，不产生额外占用
        change2, errors, _ = crud.perform_change_review(db, change.id, schemas.ChangeRequestReview(
            status=ChangeStatus.APPROVED, reviewer="调度员"))
        assert change2 is None and errors, "重复审批应被拒绝"
        print(f"✓ 重复审批被拒绝: {errors[0]}")

        # 执行 → 场次换绑新组合，旧组合释放
        result = crud.execute_change_request(db, change.id, "调度员")
        assert result.success, f"执行失败: {result.errors}"
        db.refresh(s_c)
        assert s_c.start_time.hour == 15, "场次时间应已更新"
        holds_c = active_holds(db, session_id=s_c.id)
        assert len(holds_c) == 1 and holds_c[0].start_time.hour == 15
        assert holds_c[0].status == HoldStatus.CONFIRMED
        avail = crud.get_resource_availability(
            db, device_res.id,
            tomorrow.replace(hour=14, minute=0), tomorrow.replace(hour=15, minute=0))
        assert avail.available_quantity == 5, "旧组合设备应已释放"
        print("✓ 执行完成：新组合已确认到场次，旧组合释放")

        # 重复执行 → 幂等，不重复占用
        result2 = crud.execute_change_request(db, change.id, "调度员")
        holds_c = active_holds(db, session_id=s_c.id)
        assert len(holds_c) == 1, "重复执行不得产生额外暂占"
        avail = crud.get_resource_availability(
            db, device_res.id,
            tomorrow.replace(hour=15, minute=0), tomorrow.replace(hour=17, minute=0))
        assert avail.held_quantity == 3, "重复执行不得重复占用设备"
        print("✓ 重复执行幂等，设备容量未重复占用")

        test_header("8. 取消与拒绝：释放暂占")
        # 取消：场次A(18-20点,设备2)申请改期后取消
        change_c1, errors, _ = crud.submit_change_request(db, schemas.ChangeRequestCreate(
            session_id=s_a.id, requester="李老师", change_type=ChangeType.TIME,
            new_start_time=tomorrow.replace(hour=20, minute=30),
            new_end_time=tomorrow.replace(hour=22, minute=0)))
        assert change_c1, f"提交失败: {errors}"
        change_c1, errors = crud.cancel_change_request(db, change_c1.id, "李老师")
        assert change_c1.status == ChangeStatus.CANCELLED
        assert len(active_holds(db, change_request_id=change_c1.id)) == 0, "取消后暂占应释放"
        change_c1b, errors = crud.cancel_change_request(db, change_c1.id, "李老师")
        assert change_c1b.status == ChangeStatus.CANCELLED, "重复取消应幂等"
        print("✓ 取消变更释放暂占，重复取消幂等")

        # 拒绝：场次A再次申请后被拒绝
        change_c2, errors, _ = crud.submit_change_request(db, schemas.ChangeRequestCreate(
            session_id=s_a.id, requester="李老师", change_type=ChangeType.TIME,
            new_start_time=tomorrow.replace(hour=20, minute=30),
            new_end_time=tomorrow.replace(hour=22, minute=0)))
        assert change_c2, f"提交失败: {errors}"
        change_c2, errors, _ = crud.perform_change_review(db, change_c2.id, schemas.ChangeRequestReview(
            status=ChangeStatus.REJECTED, reviewer="调度员", review_comment="时段太晚了"))
        assert change_c2.status == ChangeStatus.REJECTED
        assert len(active_holds(db, change_request_id=change_c2.id)) == 0, "拒绝后暂占应释放"
        print("✓ 审核拒绝释放暂占")

        test_header("9. 超时过期：暂占超时未确认自动释放（含重启后清理）")
        hold, errors, conflicts = crud.acquire_resource_hold(
            db, tomorrow.replace(hour=9, minute=0), tomorrow.replace(hour=11, minute=0),
            [{"resource_id": device_res.id, "quantity": 2}], ttl_minutes=1)
        assert hold and not conflicts
        db.commit()
        # 模拟时间流逝：强制过期后执行清理（服务重启时也会执行同样逻辑）
        hold.expires_at = datetime.now() - timedelta(minutes=1)
        db.commit()
        expired = crud.cleanup_expired_holds(db)
        assert expired == 1, f"应清理1条超时暂占，实际{expired}"
        db.refresh(hold)
        assert hold.status == HoldStatus.EXPIRED
        avail = crud.get_resource_availability(
            db, device_res.id,
            tomorrow.replace(hour=9, minute=0), tomorrow.replace(hour=11, minute=0))
        assert avail.available_quantity == 5, "过期暂占不得继续占用容量"
        print("✓ 超时暂占已过期清理，容量释放")

        test_header("10. 暂占过期后被他人占用 → 审批时明确失败（不再静默通过）")
        change_e, errors, _ = crud.submit_change_request(db, schemas.ChangeRequestCreate(
            session_id=s_a.id, requester="王老师", change_type=ChangeType.TIME,
            new_start_time=tomorrow.replace(hour=9, minute=0),
            new_end_time=tomorrow.replace(hour=11, minute=0),
            new_device_sets_needed=4))
        assert change_e, f"提交失败: {errors}"
        # 强制该变更的暂占过期，随后另一场次抢走设备
        db.query(ResourceHold).filter(ResourceHold.change_request_id == change_e.id).update(
            {"expires_at": datetime.now() - timedelta(minutes=1)})
        db.commit()
        crud.cleanup_expired_holds(db)
        s_f, errors, conflicts = crud.create_session_with_holds(db, make_session_in(
            "青铜研学F", theme.id, venue.id,
            tomorrow.replace(hour=9, minute=0, second=0, microsecond=0),
            tomorrow.replace(hour=11, minute=0, second=0, microsecond=0),
            device=4, aids=0))
        assert s_f, f"场次F应创建成功: {errors}"
        change_e2, errors, conflicts = crud.perform_change_review(db, change_e.id, schemas.ChangeRequestReview(
            status=ChangeStatus.APPROVED, reviewer="调度员"))
        assert change_e2 is None and conflicts, "设备已被抢走，审批必须失败并返回冲突"
        db.refresh(change_e)
        assert change_e.status == ChangeStatus.PENDING, "审批失败后变更应保持待审核"
        print(f"✓ 审批时重新校验，资源不足明确失败: {errors[0]}")
        # 清理该变更，避免影响后续用例
        crud.cancel_change_request(db, change_e.id, "王老师")

        test_header("11. 排定时暂占已过期且资源被占 → 排定失败且不误改其他字段")
        s_g, errors, conflicts = crud.create_session_with_holds(db, make_session_in(
            "青铜研学G", theme.id, venue.id,
            tomorrow.replace(hour=12, minute=0, second=0, microsecond=0),
            tomorrow.replace(hour=13, minute=0, second=0, microsecond=0),
            device=1, aids=0))
        assert s_g, f"场次G创建失败: {errors}"
        db.query(ResourceHold).filter(ResourceHold.session_id == s_g.id).update(
            {"expires_at": datetime.now() - timedelta(minutes=1)})
        db.commit()
        crud.cleanup_expired_holds(db)
        # 场次H抢走该时段全部设备
        s_h, errors, conflicts = crud.create_session_with_holds(db, make_session_in(
            "青铜研学H", theme.id, venue.id,
            tomorrow.replace(hour=12, minute=0, second=0, microsecond=0),
            tomorrow.replace(hour=13, minute=0, second=0, microsecond=0),
            device=5, aids=0))
        assert s_h, f"场次H创建失败: {errors}"
        s_g2, errors, conflicts = crud.update_session(db, s_g.id, schemas.SessionUpdate(
            status=SessionStatus.SCHEDULED, title="青铜研学G-改名"))
        assert conflicts, "设备已被H占满，排定必须失败"
        db.refresh(s_g)
        assert s_g.status == SessionStatus.DRAFT, "排定失败应保持草稿"
        assert s_g.title == "青铜研学G", "排定失败时其他字段不得被修改"
        print(f"✓ 排定被资源校验拦截: {errors[0]}")

        test_header("12. 并发场次：多线程抢同一资源池，容量不超卖")
        race_res = crud.create_resource(db, schemas.ResourceCreate(
            name="限量体验设备", resource_type=ResourceType.DEVICE_SET, total_quantity=3))
        slot_start = tomorrow.replace(hour=9, minute=0, second=0, microsecond=0)
        slot_end = tomorrow.replace(hour=11, minute=0, second=0, microsecond=0)

        def try_acquire(_):
            tdb = SessionLocal()
            try:
                h, errs, confs = crud.acquire_resource_hold(
                    tdb, slot_start, slot_end,
                    [{"resource_id": race_res.id, "quantity": 1}])
                if errs or confs:
                    tdb.rollback()
                    return False
                tdb.commit()
                return True
            finally:
                tdb.close()

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(try_acquire, range(8)))
        assert sum(results) == 3, f"容量3应恰好成功3次，实际{sum(results)}"
        avail = crud.get_resource_availability(db, race_res.id, slot_start, slot_end)
        assert avail.held_quantity == 3 and avail.available_quantity == 0
        print(f"✓ 8路并发抢3套设备：成功{sum(results)}次，占用{avail.held_quantity}/3，未超卖")

        test_header("13. 未配置资源池时给出可解释冲突")
        s_g, errors, conflicts = crud.create_session_with_holds(db, make_session_in(
            "茶文化体验", theme2.id, venue.id,
            tomorrow.replace(hour=9, minute=0, second=0, microsecond=0),
            tomorrow.replace(hour=10, minute=0, second=0, microsecond=0),
            device=0, aids=2))
        assert s_g is None and len(conflicts) == 1
        assert conflicts[0].resource_type == ResourceType.TEACHING_AID
        assert conflicts[0].total_quantity == 0
        print(f"✓ 未配置资源冲突可解释: {conflicts[0].message}")

        test_header("14. 场次取消/完成：释放全部暂占")
        _, errors, _ = crud.update_session(db, s_c.id, schemas.SessionUpdate(
            status=SessionStatus.CANCELLED))
        assert not errors, f"取消失败: {errors}"
        assert len(active_holds(db, session_id=s_c.id)) == 0, "取消后暂占应全部释放"
        avail = crud.get_resource_availability(
            db, device_res.id,
            tomorrow.replace(hour=15, minute=0), tomorrow.replace(hour=17, minute=0))
        assert avail.available_quantity == 5, "取消后设备应全部回收"
        print("✓ 场次取消后暂占全部释放")

        test_header("所有资源暂占测试通过！功能验证完成")
        print("""
核心能力验证清单:
  ✓ 草稿创建即按时间段暂占展厅/设备/教具
  ✓ 全有或全无：任一资源不足返回可解释冲突集合，无部分占用泄漏
  ✓ 冲突集合含资源、时段、需求量、可用量与占用方明细
  ✓ 变更预审暂占新组合，阻止其他场次抢走资源
  ✓ 审批确认暂占（过期则重新校验，不足则明确失败）
  ✓ 执行转移暂占并释放旧组合，重复执行幂等
  ✓ 取消/拒绝释放暂占，重复取消幂等
  ✓ 超时暂占自动过期（服务重启后同样清理）
  ✓ 改期先验证新组合成功后再释放旧组合
  ✓ 并发暂占原子核算，资源容量不超卖
        """)

    except AssertionError as e:
        print(f"\n✗ 验证失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    except Exception as e:
        print(f"\n✗ 测试异常: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
