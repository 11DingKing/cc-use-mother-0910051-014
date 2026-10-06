#!/usr/bin/env python3
"""多资源统一暂存端到端验证（独立临时数据库，不影响开发库）。"""
import os
import tempfile
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp.name}"
os.environ["DEVICE_POOL_CAPACITY"] = "3"
os.environ["HOLD_SWEEP_INTERVAL_SECONDS"] = "3600"

from fastapi.testclient import TestClient
from app.main import app
from app.database import SessionLocal
from app.services import resource_holds
from app.models import ResourceHold, HoldPurpose, ResourceType

DAY = datetime(2026, 11, 1, 10, 0, 0)


def t(h1, m1=0, h2=None, m2=0):
    s = DAY.replace(hour=h1, minute=m1)
    e = DAY.replace(hour=(h2 if h2 is not None else h1 + 2), minute=m2)
    return s.isoformat(), e.isoformat()


def run(client):
    def j(r):
        return r.json()

    passed = []

    def check(name, cond, extra=""):
        assert cond, f"FAIL: {name} {extra}"
        passed.append(name)
        print(f"  ✓ {name}")

    # ---------- 基础数据 ----------
    print("== 基础数据 ==")
    t1 = j(client.post("/api/themes", json={"name": "主题甲", "category": "x"}))["id"]
    t2 = j(client.post("/api/themes", json={"name": "主题乙", "category": "x"}))["id"]
    venues = []
    for i in range(1, 12):
        v = j(client.post("/api/venues", json={"name": f"展厅{i}", "venue_type": "展厅", "capacity": 30}))
        venues.append(v["id"])
    v1, v2, v3 = venues[0], venues[1], venues[2]
    sc = j(client.post("/api/schools", json={"name": "小学"}))["id"]
    staff = []
    for nm in ("讲解员A", "讲解员B", "讲解员C"):
        s = j(client.post("/api/staff", json={
            "name": nm, "staff_type": "讲解员",
            "themes": [{"theme_id": t1, "proficiency_level": 5}, {"theme_id": t2, "proficiency_level": 5}],
            "venues": [{"venue_id": v, "is_certified": True} for v in venues],
        }))
        staff.append(s["id"])

    av = client.post("/api/resources/pools/ensure").json()
    check("资源台账含设备池/展厅池/教具池",
          {p["resource_type"] for p in av} == {"无线设备套装", "展厅", "主题教具"})
    check("设备池容量=3", next(p["capacity"] for p in av if p["resource_type"] == "无线设备套装") == 3)

    # ---------- 1. 草稿原子暂占 ----------
    print("== 1. 草稿暂占 ==")
    s1, e1 = t(10, 0, 12, 0)
    r = client.post("/api/sessions", json={
        "title": "A", "theme_id": t1, "venue_id": v1, "session_type": "研学实践",
        "start_time": s1, "end_time": e1, "audience_type": "学校",
        "school_id": sc, "guides_needed": 1, "device_sets_needed": 2})
    check("创建场次A成功", r.status_code == 200, r.text)
    A = r.json()
    check("A返回暂占组", bool(A["resource_group_id"]))

    holds = client.get("/api/resources/holds", params={"session_id": A["id"]}).json()
    check("A有3条暂占(展厅/设备/教具)", len(holds) == 3,
          str([(h["resource_type"], h["quantity"]) for h in holds]))
    check("设备暂占数量=2", next(h["quantity"] for h in holds if h["resource_type"] == "无线设备套装") == 2)

    av = client.get("/api/resources/availability", params={"start_time": s1, "end_time": e1}).json()
    v1row = next(x for x in av if x["resource_type"] == "展厅" and x["ref_id"] == v1)
    check("展厅v1余量0/已占1", v1row["held"] == 1 and v1row["available"] == 0)
    check("设备已占2/余量1", next(x for x in av if x["resource_type"] == "无线设备套装")["available"] == 1)

    # ---------- 2. 资源不足：原子冲突，不留半占 ----------
    print("== 2. 原子冲突集合 ==")
    r = client.post("/api/sessions", json={
        "title": "B", "theme_id": t2, "venue_id": v1, "session_type": "研学实践",
        "start_time": s1, "end_time": e1, "audience_type": "公众",
        "guides_needed": 0, "device_sets_needed": 1})
    check("B创建被拒409", r.status_code == 409, r.text)
    body = r.json()["detail"]
    check("返回冲突集合", isinstance(body["conflicts"], list) and len(body["conflicts"]) >= 1)
    c0 = body["conflicts"][0]
    check("冲突可解释(含容量/已占/余量/占用方)",
          all(k in c0 for k in ("capacity", "held_by_others", "available", "holders", "message")),
          str(c0))
    check("冲突类型为展厅", c0["resource_type"] == "展厅", str(c0))

    db = SessionLocal()
    n_b = db.query(ResourceHold).filter(
        ResourceHold.session_id.is_(None), ResourceHold.purpose == HoldPurpose.DRAFT).count()
    db.close()
    check("失败请求无残留草稿暂占", n_b == 0, str(n_b))

    # 设备池维度唯一短板
    r = client.post("/api/sessions", json={
        "title": "C", "theme_id": t2, "venue_id": v2, "session_type": "研学实践",
        "start_time": s1, "end_time": e1, "audience_type": "公众",
        "guides_needed": 0, "device_sets_needed": 2})
    check("C因设备不足409", r.status_code == 409, r.text)
    conf_types = {c["resource_type"] for c in r.json()["detail"]["conflicts"]}
    check("冲突只报设备一项(未误报展厅/教具)", conf_types == {"无线设备套装"}, str(conf_types))

    # ---------- 3. 预览不落库 ----------
    print("== 3. 预审预览 ==")
    r = client.post("/api/resources/preview", json={
        "venue_id": v2, "theme_id": t2, "start_time": s1, "end_time": e1, "device_sets_needed": 1})
    check("预览v2可用", r.status_code == 200 and r.json()["available"] is True, r.text)
    r = client.post("/api/resources/preview", json={
        "venue_id": v1, "theme_id": t1, "start_time": s1, "end_time": e1, "device_sets_needed": 1})
    preview_conf = {c["resource_type"] for c in r.json()["conflicts"]}
    check("预览v1撞展厅+教具但不暂占",
          r.json()["available"] is False and preview_conf == {"展厅", "主题教具"}, str(preview_conf))
    av2 = client.get("/api/resources/availability", params={"start_time": s1, "end_time": e1}).json()
    check("预览后占用量不变", next(x for x in av2 if x["resource_type"] == "展厅" and x["ref_id"] == v2)["held"] == 0)

    # ---------- 4. 并发同抢 ----------
    print("== 4. 并发不超卖 ==")
    sx, ex = t(13, 0, 13, 30)

    def create_concurrent(i):
        return client.post("/api/sessions", json={
            "title": f"并发{i}", "theme_id": t1, "venue_id": v3,
            "session_type": "研学实践", "start_time": sx, "end_time": ex,
            "audience_type": "公众", "guides_needed": 0, "device_sets_needed": 1})

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(create_concurrent, range(10)))
    check("10并发仅1个成功", len([r for r in results if r.status_code == 200]) == 1,
          str([r.status_code for r in results]))
    avx = client.get("/api/resources/availability", params={"start_time": sx, "end_time": ex}).json()
    v3row = next(x for x in avx if x["resource_type"] == "展厅" and x["ref_id"] == v3)
    check("v3占用=1未超卖", v3row["held"] == 1 and v3row["available"] == 0)

    race_themes = [j(client.post("/api/themes", json={"name": f"并发主题{k}", "category": "x"}))["id"]
                   for k in range(8)]

    def create_concurrent_dev(i):
        return client.post("/api/sessions", json={
            "title": f"设备并发{i}", "theme_id": race_themes[i], "venue_id": venues[3 + i],
            "session_type": "研学实践", "start_time": sx, "end_time": ex,
            "audience_type": "公众", "guides_needed": 0, "device_sets_needed": 1})

    with ThreadPoolExecutor(max_workers=8) as pool:
        results2 = list(pool.map(create_concurrent_dev, range(8)))
    check("设备池维度2成功(已有1,容量3)", len([r for r in results2 if r.status_code == 200]) == 2,
          str([r.status_code for r in results2]))
    avx = client.get("/api/resources/availability", params={"start_time": sx, "end_time": ex}).json()
    check("设备并发占用=3不超卖", next(x for x in avx if x["resource_type"] == "无线设备套装")["held"] == 3)

    # ---------- 4b. 容量按最大并发而非总量 ----------
    print("== 4b. 区间最大并发口径 ==")
    # 设备池在后续空闲日，容量1：两个首尾相接的场次应都能成功
    d2 = "2026-11-03"
    bx_reqs_venues = venues[10]
    px_theme = j(client.post("/api/themes", json={"name": "拼接主题", "category": "x"}))["id"]
    r1 = client.post("/api/sessions", json={
        "title": "拼接1", "theme_id": px_theme, "venue_id": venues[10],
        "session_type": "研学实践", "start_time": f"{d2}T10:00:00", "end_time": f"{d2}T11:00:00",
        "audience_type": "公众", "guides_needed": 0, "device_sets_needed": 1})
    r2 = client.post("/api/sessions", json={
        "title": "拼接2", "theme_id": px_theme, "venue_id": venues[10],
        "session_type": "研学实践", "start_time": f"{d2}T11:00:00", "end_time": f"{d2}T12:00:00",
        "audience_type": "公众", "guides_needed": 0, "device_sets_needed": 1})
    check("首尾相接两场次都成功(容量复用)", r1.status_code == 200 and r2.status_code == 200,
          f"{r1.status_code}/{r2.status_code}")
    avw = client.get("/api/resources/availability",
                     params={"start_time": f"{d2}T10:00:00", "end_time": f"{d2}T12:00:00"}).json()
    dev_peak = next(x for x in avw if x["resource_type"] == "无线设备套装")
    check("跨窗口设备峰值=1(非求和2)", dev_peak["held"] == 1 and dev_peak["available"] == 2,
          str((dev_peak["held"], dev_peak["available"])))
    # 但窗口内峰值余量为2：一次申请3套且横跨两场仍被设备拒绝
    r3 = client.post("/api/sessions", json={
        "title": "拼接3", "theme_id": px_theme, "venue_id": venues[9],
        "session_type": "研学实践", "start_time": f"{d2}T10:30:00", "end_time": f"{d2}T11:30:00",
        "audience_type": "公众", "guides_needed": 0, "device_sets_needed": 3})
    check("横跨窗口申请3套被设备拒绝", r3.status_code == 409 and
          any(c["resource_type"] == "无线设备套装" for c in r3.json()["detail"]["conflicts"]), r3.text)

    # ---------- 5. 排定确认 ----------
    print("== 5. 排定确认暂占 ==")
    r = client.post(f"/api/sessions/{A['id']}/assignments", json={"staff_id": staff[0], "role": "讲解员"})
    check("为A分配讲解员", r.status_code == 200, r.text)
    A = client.get(f"/api/sessions/{A['id']}").json()
    check("A已排定", A["status"] == "已排定", A["status"])
    holds = client.get("/api/resources/holds", params={"session_id": A["id"]}).json()
    check("A暂占全部已确认", all(h["status"] == "已确认" for h in holds),
          str([h["status"] for h in holds]))

    # ---------- 6. 变更预审/冲突 ----------
    print("== 6. 变更预审暂占与冲突 ==")
    s2, e2 = t(14, 0, 16, 0)
    client.post("/api/sessions", json={
        "title": "占v1", "theme_id": t1, "venue_id": v1, "session_type": "研学实践",
        "start_time": s2, "end_time": e2, "audience_type": "公众",
        "guides_needed": 0, "device_sets_needed": 1})
    r = client.post("/api/changes", json={
        "session_id": A["id"], "requester": "x", "change_type": "时间变更",
        "new_start_time": s2, "new_end_time": e2})
    check("变更撞v1被拒409", r.status_code == 409, r.text)
    check("冲突集合含展厅", any(c["resource_type"] == "展厅" for c in r.json()["detail"]["conflicts"]))
    db = SessionLocal()
    n_ch = db.query(ResourceHold).filter(ResourceHold.purpose == HoldPurpose.CHANGE_REVIEW).count()
    db.close()
    check("失败变更无预审暂占残留", n_ch == 0, str(n_ch))

    s3, e3 = t(16, 0, 18, 0)
    r = client.post("/api/changes", json={
        "session_id": A["id"], "requester": "x", "change_type": "时间变更",
        "new_start_time": s3, "new_end_time": e3})
    check("变更提交成功(已预审暂占)", r.status_code == 200, r.text)
    chg = r.json()
    check("变更记录预审组", bool(chg["resource_group_id"]))
    cr_holds = client.get("/api/resources/holds", params={"change_request_id": chg["id"]}).json()
    check("预审暂占3条且为暂占中", len(cr_holds) == 3 and all(h["status"] == "暂占中" for h in cr_holds),
          str([(h["resource_type"], h["status"]) for h in cr_holds]))

    av3 = client.get("/api/resources/availability", params={"start_time": s3, "end_time": e3}).json()
    check("新时段v1被预审暂占", next(x for x in av3 if x["resource_type"] == "展厅" and x["ref_id"] == v1)["held"] == 1)
    av3b = client.get("/api/resources/availability", params={"start_time": s1, "end_time": e1}).json()
    check("旧时段v1仍被旧组合占用", next(x for x in av3b if x["resource_type"] == "展厅" and x["ref_id"] == v1)["held"] == 1)

    r = client.post("/api/changes", json={
        "session_id": A["id"], "requester": "x", "change_type": "时间变更",
        "new_start_time": s3, "new_end_time": e3})
    check("重复变更被拒", r.status_code == 400)

    # ---------- 7. 审批确认 / 重复审批 ----------
    print("== 7. 审批与重复审批 ==")
    r = client.put(f"/api/changes/{chg['id']}/review", json={"status": "已通过", "reviewer": "r"})
    check("审批通过", r.status_code == 200, r.text)
    total_after_approve = len(client.get("/api/resources/holds").json())
    r = client.put(f"/api/changes/{chg['id']}/review", json={"status": "已通过", "reviewer": "r"})
    check("重复审批被业务拒绝", r.status_code == 400)
    check("重复审批后暂占数量不变", len(client.get("/api/resources/holds").json()) == total_after_approve)
    cr_holds = client.get("/api/resources/holds", params={"change_request_id": chg["id"]}).json()
    check("预审组已确认", all(h["status"] == "已确认" for h in cr_holds))

    # ---------- 8. 执行换组 ----------
    print("== 8. 执行换组 ==")
    r = client.post(f"/api/changes/{chg['id']}/execute", json={"operator": "op"})
    check("执行成功", r.status_code == 200 and r.json()["success"], r.text)
    A2 = client.get(f"/api/sessions/{A['id']}").json()
    check("A时间已改到16:00", A2["start_time"].startswith("2026-11-01T16:00"))
    av4 = client.get("/api/resources/availability", params={"start_time": s1, "end_time": e1}).json()
    check("旧时段v1已释放", next(x for x in av4 if x["resource_type"] == "展厅" and x["ref_id"] == v1)["held"] == 0)
    av5 = client.get("/api/resources/availability", params={"start_time": s3, "end_time": e3}).json()
    check("新时段v1被正式占用", next(x for x in av5 if x["resource_type"] == "展厅" and x["ref_id"] == v1)["held"] == 1)
    check("A只剩3条活跃暂占(旧组已释放)",
          len(client.get("/api/resources/holds", params={"session_id": A["id"]}).json()) == 3)

    r = client.post(f"/api/changes/{chg['id']}/execute", json={"operator": "op"})
    check("重复执行幂等成功", r.status_code == 200 and r.json()["success"], r.text)
    check("重复执行不产生新暂占",
          len(client.get("/api/resources/holds", params={"session_id": A["id"]}).json()) == 3)

    # ---------- 9. 拒绝/取消释放 ----------
    print("== 9. 拒绝/取消释放 ==")
    sd, ed = t(9, 0, 9, 30)
    s0, e0 = t(8, 0, 8, 30)

    def held_venue(venue_id, s, e):
        rows = client.get("/api/resources/availability", params={"start_time": s, "end_time": e}).json()
        return next(x for x in rows if x["resource_type"] == "展厅" and x["ref_id"] == venue_id)["held"]

    D = client.post("/api/sessions", json={
        "title": "D", "theme_id": t1, "venue_id": venues[7], "session_type": "研学实践",
        "start_time": s0, "end_time": e0, "audience_type": "公众",
        "guides_needed": 0, "device_sets_needed": 1}).json()
    ch_d = client.post("/api/changes", json={
        "session_id": D["id"], "requester": "x", "change_type": "时间变更",
        "new_start_time": sd, "new_end_time": ed}).json()
    check("拒绝前预审占用", held_venue(venues[7], sd, ed) == 1)
    r = client.put(f"/api/changes/{ch_d['id']}/review", json={"status": "已拒绝", "reviewer": "r"})
    check("拒绝成功", r.status_code == 200)
    check("拒绝后预审暂占释放", held_venue(venues[7], sd, ed) == 0)

    E = client.post("/api/sessions", json={
        "title": "E", "theme_id": t2, "venue_id": venues[8], "session_type": "研学实践",
        "start_time": s0, "end_time": e0, "audience_type": "公众",
        "guides_needed": 0, "device_sets_needed": 1}).json()
    ch_e = client.post("/api/changes", json={
        "session_id": E["id"], "requester": "x", "change_type": "时间变更",
        "new_start_time": sd, "new_end_time": ed}).json()
    r = client.post(f"/api/changes/{ch_e['id']}/cancel", json={"operator": "op"})
    check("取消变更成功", r.status_code == 200, r.text)
    check("取消后预审暂占释放", held_venue(venues[8], sd, ed) == 0)

    check("取消前E占用原时段", held_venue(venues[8], s0, e0) == 1)
    r = client.put(f"/api/sessions/{E['id']}", json={"status": "已取消"})
    check("取消场次", r.status_code == 200)
    check("取消场次后暂占全释放",
          len(client.get("/api/resources/holds", params={"session_id": E["id"]}).json()) == 0)

    # ---------- 10. TTL 超时 + 重启清理 ----------
    print("== 10. 超时清理 ==")
    F = client.post("/api/sessions", json={
        "title": "F", "theme_id": t1, "venue_id": venues[9], "session_type": "研学实践",
        "start_time": s1, "end_time": e1, "audience_type": "公众",
        "guides_needed": 0, "device_sets_needed": 1}).json()
    gid = F["resource_group_id"]
    db = SessionLocal()
    db.query(ResourceHold).filter(ResourceHold.group_id == gid).update(
        {"expires_at": datetime.now() - timedelta(seconds=1)})
    db.commit()
    db.close()
    check("过期暂占不再计入容量", held_venue(venues[9], s1, e1) == 0)
    r = client.post("/api/resources/sweep")
    check("手动清扫清理>=1", r.status_code == 200 and r.json()["expired"] >= 1, r.text)
    n_exp = len(client.get("/api/resources/holds", params={"session_id": F["id"], "status": "已过期"}).json())
    check("F暂占被标记已过期", n_exp == 3, str(n_exp))
    r = client.post("/api/resources/sweep")
    check("重复清扫幂等", r.json()["expired"] == 0)

    # ---------- 11. 改期失败不破坏旧组合 ----------
    print("== 11. 改期失败保留旧组合 ==")
    db = SessionLocal()
    g_reqs = resource_holds.build_session_requirements(db, venues[10], t1, 1)
    gid_g = resource_holds.acquire_holds(
        db, g_reqs, datetime(2026, 11, 2, 9, 0), datetime(2026, 11, 2, 10, 0),
        HoldPurpose.DRAFT, 1800)
    block_reqs = resource_holds.build_session_requirements(db, venues[10], t2, 1)
    resource_holds.acquire_holds(
        db, block_reqs, datetime(2026, 11, 2, 10, 0), datetime(2026, 11, 2, 11, 0),
        HoldPurpose.DRAFT, 1800)
    swap_failed = False
    try:
        resource_holds.swap_holds(
            db, g_reqs, datetime(2026, 11, 2, 9, 30), datetime(2026, 11, 2, 11, 0),
            gid_g, 1800, session_id=999)
    except resource_holds.ResourceConflict:
        swap_failed = True
    check("改期冲突时抛 ResourceConflict", swap_failed)
    rows_g = db.query(ResourceHold).filter(ResourceHold.group_id == gid_g).all()
    check("旧组合在改期失败后仍活跃", all(r.status in resource_holds.ACTIVE_STATUSES for r in rows_g))
    db.close()

    # ---------- 12. 容量调整下限保护 ----------
    print("== 12. 容量管理 ==")
    dev_pool = next(p for p in client.get("/api/resources/pools").json()
                    if p["resource_type"] == "无线设备套装")
    r = client.put(f"/api/resources/pools/{dev_pool['id']}", json={"capacity": 2})
    check("设备池不能降到当前并发以下", r.status_code == 409, r.text)
    r = client.put(f"/api/resources/pools/{dev_pool['id']}", json={"capacity": 5})
    check("设备池可扩容", r.status_code == 200 and r.json()["capacity"] == 5)

    print(f"\n全部 {len(passed)} 项检查通过 ✅")
    return passed


if __name__ == "__main__":
    try:
        with TestClient(app) as client:
            run(client)
    finally:
        try:
            os.unlink(_tmp.name)
        except OSError:
            pass
