"""过期暂占清理：服务启动时执行一次（覆盖重启后的过期清理），随后后台守护线程
按固定间隔执行 TTL 与历史场次清扫。"""
from __future__ import annotations

import threading

from app.config import settings
from app.database import SessionLocal
from app.services import resource_holds

_started = False
_lock = threading.Lock()


def sweep_once() -> dict:
    db = SessionLocal()
    try:
        return resource_holds.sweep_expired_holds(db)
    finally:
        db.close()


def _sweep_loop() -> None:
    import time
    interval = max(5, settings.HOLD_SWEEP_INTERVAL_SECONDS)
    while True:
        time.sleep(interval)
        try:
            sweep_once()
        except Exception:
            # 清扫失败不影响主服务，下一轮继续
            pass


def start_hold_maintenance() -> None:
    """幂等启动：先同步清扫一次（服务重启后的过期清理），再启动后台线程。"""
    global _started
    with _lock:
        if _started:
            return
        # 重启清理：把重启期间超时的暂占立即标记释放
        try:
            sweep_once()
        except Exception:
            pass
        thread = threading.Thread(
            target=_sweep_loop, name="resource-hold-sweeper", daemon=True
        )
        thread.start()
        _started = True
