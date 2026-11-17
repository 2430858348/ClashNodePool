"""服务生命周期：绑定到浏览器页面。

模型：
  前端每 15 秒发一次心跳；关闭页面时用 sendBeacon 发一个「再见」。
  服务端看门狗据此判断：
    - 收到「再见」→ 宽限 N 秒（容错刷新），期间有任何心跳就撤销
    - 长时间没心跳（后台标签页被浏览器限流也不会误杀）→ 兜底关闭

只有通过桌面图标（open-panel.bat）启动时才启用，方便 run-server.bat 手动长跑调试。
"""
from __future__ import annotations

import asyncio
import os
import time

from . import db

# 由启动脚本注入的环境变量
AUTOSTOP = os.environ.get("NP_AUTOSTOP") == "1"

_state: dict = {
    "last_beat": 0.0,
    "pending_stop_at": 0.0,
    "ever_connected": False,
    "stopping": False,
}
_server = None
_cfg_cache: tuple[float, dict] = (0.0, {})


def attach(server) -> None:
    """把 uvicorn 的 Server 实例交给看门狗，用于优雅退出。"""
    global _server
    _server = server


def settings_cached() -> dict:
    global _cfg_cache
    now = time.time()
    if now - _cfg_cache[0] > 10:
        _cfg_cache = (now, db.get_settings())
    return _cfg_cache[1]


def beat() -> dict:
    now = time.time()
    _state["last_beat"] = now
    _state["pending_stop_at"] = 0.0  # 有新页面在，撤销待关闭
    _state["ever_connected"] = True
    return {"ok": True, "ts": int(now)}


def bye() -> dict:
    cfg = settings_cached()
    grace = float(cfg.get("shutdown_grace_sec", 10))
    _state["pending_stop_at"] = time.time() + grace
    return {"ok": True, "stop_in": int(grace)}


def status() -> dict:
    now = time.time()
    cfg = settings_cached()
    return {
        "autostop": AUTOSTOP and bool(cfg.get("auto_stop_on_page_close", True)),
        "ever_connected": _state["ever_connected"],
        "idle_sec": int(now - _state["last_beat"]) if _state["last_beat"] else None,
        "heartbeat_timeout": int(cfg.get("heartbeat_timeout_sec", 150)),
        "pending_stop_in": (
            max(0, int(_state["pending_stop_at"] - now)) if _state["pending_stop_at"] else None
        ),
        "stopping": _state["stopping"],
    }


def _hard_exit() -> None:
    """兜底强制退出。

    uvicorn 的优雅退出会被没断开的 keep-alive 连接拖住（graceful timeout 默认无限），
    实测会出现「端口已关但进程还在」的僵尸。这里直接结束进程，
    SQLite 用 WAL 模式，异常退出也不会损坏数据。
    """
    try:
        db.execute("PRAGMA wal_checkpoint(PASSIVE)")
    except Exception:
        pass
    os._exit(0)


def _request_shutdown(reason: str) -> None:
    if _state["stopping"]:
        return
    _state["stopping"] = True
    db.log("info", "lifecycle", reason)
    if _server is not None:
        _server.should_exit = True
    try:
        asyncio.get_event_loop().call_later(3.0, _hard_exit)
    except Exception:
        _hard_exit()


def enabled(cfg: dict | None = None) -> bool:
    cfg = cfg if cfg is not None else settings_cached()
    return AUTOSTOP and bool(cfg.get("auto_stop_on_page_close", True))


def evaluate(now: float, cfg: dict) -> str | None:
    """纯判断：该不该停？返回停止原因，None 表示继续跑。便于单测。"""
    if _state["pending_stop_at"] and now >= _state["pending_stop_at"]:
        return "页面已关闭，服务自动停止"
    timeout = float(cfg.get("heartbeat_timeout_sec", 150))
    last = _state["last_beat"]
    if last and (now - last) > timeout:
        return f"心跳超时 {int(now - last)} 秒，服务自动停止"
    return None


async def watchdog() -> None:
    """每秒检查一次。关闭自动停止时立即返回，不占资源。"""
    _state["last_beat"] = time.time()  # 启动宽限：等浏览器打开
    while True:
        await asyncio.sleep(1.0)
        cfg = settings_cached()
        if not enabled(cfg):
            return
        reason = evaluate(time.time(), cfg)
        if reason:
            _request_shutdown(reason)
            return
