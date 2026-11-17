"""FastAPI 入口：面板、订阅接口、管理 API。"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import clash, db, lifecycle, pipeline, pool, tokens
from .config import APP_NAME, BASE_DIR, DATA_DIR, HOST, PORT, WEB_DIR


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    pipeline.seed_sources()
    # 首次启动送一张默认订阅，免得面板是空的
    if not tokens.list_all():
        tokens.create("默认订阅", db.get_settings()["default_token_ttl_hours"])
        db.log("info", "token", "已生成默认订阅链接，可在面板复制")

    tasks = [
        asyncio.create_task(pipeline.scheduler_loop()),
        asyncio.create_task(lifecycle.watchdog()),
    ]
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass


app = FastAPI(title=APP_NAME, version="1.0.0", lifespan=lifespan,
              docs_url=None, redoc_url=None)


# ---------------- 面板 ----------------

@app.get("/", include_in_schema=False)
def index():
    f = WEB_DIR / "index.html"
    if not f.exists():
        return PlainTextResponse("面板文件缺失：web/index.html", status_code=500)
    return FileResponse(f, headers={"Cache-Control": "no-store"})


@app.get("/favicon.ico", include_in_schema=False)
@app.get("/icon.ico", include_in_schema=False)
def favicon():
    """浏览器标签页图标：直接复用桌面快捷方式用的同一个 icon.ico，保持单一来源。"""
    f = BASE_DIR / "icon.ico"
    if not f.exists():
        return Response(status_code=204)
    return FileResponse(
        f,
        media_type="image/x-icon",
        headers={"Cache-Control": "public, max-age=86400"},
    )


if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


# ---------------- 生命周期（页面心跳） ----------------

@app.post("/api/beat", include_in_schema=False)
def api_beat():
    """面板开着就定时打这个，服务靠它判断「还有人在看」。"""
    return lifecycle.beat()


@app.post("/api/bye", include_in_schema=False)
def api_bye():
    """页面关闭时用 sendBeacon 发一次，触发宽限倒计时。"""
    return lifecycle.bye()


# ---------------- 订阅（核心） ----------------

@app.get("/sub/{token}", include_in_schema=False)
def subscription(token: str):
    try:
        rec = tokens.verify(token)
    except tokens.TokenError as e:
        tokens.touch_reject(token, e.message)
        return PlainTextResponse(f"订阅不可用：{e.message}\n", status_code=404)

    cfg = db.get_settings()
    nodes = pool.alive_nodes(int(cfg.get("max_nodes", 200)))
    if not nodes:
        # 池子空时绝不下发空 proxies，否则 Clash 会把你现有节点清掉
        db.log("warn", "sub", "节点池为空，拒绝下发配置（保护客户端现有节点）")
        return PlainTextResponse(
            "节点池当前为空，本次更新已拒绝以避免清空你现有的节点。\n",
            status_code=503,
        )

    body = clash.build_config(nodes, cfg)
    # Content-Type 由 headers 统一给出，避免 Starlette 再追加一次 charset
    return Response(content=body, headers=clash.subscription_headers(rec["expires_ts"]))


# ---------------- 状态 ----------------

@app.get("/api/status")
def api_status():
    cfg = db.get_settings()
    st = pipeline.state()
    srcs = pipeline.list_sources()
    tks = tokens.list_all()
    return {
        "now": int(time.time()),
        "pool": pool.stats(),
        "sources": {
            "total": len(srcs),
            "enabled": sum(1 for s in srcs if s["enabled"]),
        },
        "tokens": {
            "total": len(tks),
            "active": sum(1 for t in tks if t["state"] == "active"),
        },
        "pipeline": {
            "running": st["running"],
            "last_start": st["last_start"],
            "last_end": st["last_end"],
            "last_trigger": st["last_trigger"],
            "last_result": st["last_result"],
        },
        "settings": cfg,
        "lifecycle": lifecycle.status(),
        "version": "1.0.0",
    }


@app.get("/api/nodes")
def api_nodes(alive: int = 1, limit: int = 300, q: str = ""):
    nodes = pool.alive_nodes(max(1, min(limit, 2000))) if alive else []
    if q:
        kw = q.lower()
        nodes = [n for n in nodes
                 if kw in n["name"].lower() or kw in n["server"].lower()
                 or kw in n["type"].lower() or kw in n["source_name"].lower()]
    return {"count": len(nodes), "nodes": nodes}


@app.post("/api/refresh")
async def api_refresh():
    if pipeline.state()["running"]:
        return JSONResponse({"started": False, "reason": "上一轮还在跑"}, status_code=409)
    ok = pipeline.trigger("manual")
    if not ok:
        return JSONResponse({"started": False, "reason": "启动失败"}, status_code=409)
    return {"started": True}


@app.get("/api/logs")
def api_logs(limit: int = 200, level: str = ""):
    logs = db.recent_logs(max(1, min(limit, 1000)))
    if level:
        logs = [x for x in logs if x["level"] == level]
    return {"count": len(logs), "logs": logs}


# ---------------- 源管理 ----------------

@app.get("/api/sources")
def api_sources():
    return {"sources": pipeline.list_sources()}


class SourcePatch(BaseModel):
    enabled: bool | None = None
    priority: int | None = Field(default=None, ge=0, le=9999)


@app.patch("/api/sources/{sid}")
def api_source_patch(sid: int, body: SourcePatch):
    row = db.query_one("SELECT id FROM sources WHERE id=?", (sid,))
    if not row:
        raise HTTPException(404, "源不存在")
    if body.enabled is not None:
        db.execute("UPDATE sources SET enabled=? WHERE id=?", (1 if body.enabled else 0, sid))
    if body.priority is not None:
        db.execute("UPDATE sources SET priority=? WHERE id=?", (body.priority, sid))
    return {"ok": True}


# ---------------- 令牌管理 ----------------

@app.get("/api/tokens")
def api_tokens(request: Request):
    base = str(request.base_url).rstrip("/")
    out = []
    for t in tokens.list_all():
        t["url"] = f"{base}/sub/{t['token']}"
        out.append(t)
    return {"tokens": out}


class TokenCreate(BaseModel):
    label: str = ""
    ttl_hours: float = Field(default=168, gt=0, le=8760)


@app.post("/api/tokens")
def api_token_create(body: TokenCreate, request: Request):
    t = tokens.create(body.label, body.ttl_hours)
    base = str(request.base_url).rstrip("/")
    t["url"] = f"{base}/sub/{t['token']}"
    return t


class TokenPatch(BaseModel):
    extend_hours: float | None = Field(default=None, gt=0, le=8760)
    extend_from_now: bool = False
    revoked: bool | None = None


@app.patch("/api/tokens/{token}")
def api_token_patch(token: str, body: TokenPatch, request: Request):
    try:
        if body.extend_hours:
            tokens.extend(token, body.extend_hours, body.extend_from_now)
        if body.revoked is not None:
            tokens.revoke(token, body.revoked)
        rec = tokens.get(token)
    except tokens.TokenError as e:
        raise HTTPException(404, e.message)
    if not rec:
        raise HTTPException(404, "订阅不存在")
    rec["url"] = f"{str(request.base_url).rstrip('/')}/sub/{token}"
    return rec


@app.delete("/api/tokens/{token}")
def api_token_delete(token: str):
    tokens.delete(token)
    return {"ok": True}


def _ensure_streams(force_file: bool = False) -> None:
    """无窗口运行时没有控制台，日志重定向到 data/server.log。"""
    if not force_file and sys.stdout is not None and sys.stderr is not None:
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log_path = DATA_DIR / "server.log"
    try:
        if log_path.exists() and log_path.stat().st_size > 2 * 1024 * 1024:
            log_path.unlink()
    except OSError:
        pass
    f = open(log_path, "a", encoding="utf-8", buffering=1)
    if sys.stdout is None or force_file:
        sys.stdout = f
    if sys.stderr is None or force_file:
        sys.stderr = f


def spawn_detached() -> int:
    """把自己以「无窗口、脱离父进程」的方式再启动一份，返回子进程 PID。

    不用 pythonw.exe：venv 里的 pythonw 是个启动器存根，Popen 拿到的是存根 PID，
    真实服务是它的子进程，后面做清理/判断会错位。直接用 sys.executable 配
    DETACHED_PROCESS，进程不创建也不继承控制台，效果一样且 PID 准确。
    """
    import subprocess

    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    env = dict(os.environ)
    env["NP_AUTOSTOP"] = "1"
    env["NP_QUIET"] = "1"
    p = subprocess.Popen(
        [sys.executable, "-m", "app.main"],
        cwd=str(BASE_DIR),
        env=env,
        creationflags=flags,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    return p.pid


def _diag(msg: str) -> None:
    """启动诊断日志。bat 里用 >nul 掩盖了输出，出问题只能靠这里查。"""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(DATA_DIR / "daemon.log", "a", encoding="utf-8") as f:
            f.write("%s pid=%d %s\n" % (time.strftime("%m-%d %H:%M:%S"), os.getpid(), msg))
    except Exception:
        pass


def main() -> None:
    if "--daemon" in sys.argv:
        try:
            pid = spawn_detached()
            _diag("daemon spawned child pid=%s" % pid)
            print(pid)
        except Exception as e:
            _diag("daemon spawn FAILED: %r" % (e,))
        return

    _ensure_streams(force_file=os.environ.get("NP_QUIET") == "1")
    _diag("server process starting")
    import uvicorn

    config = uvicorn.Config(app, host=HOST, port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    # 交给看门狗，页面关掉时它负责优雅退出
    lifecycle.attach(server)
    server.run()
    # 已优雅收尾，强制退出，避免残留线程把进程挂住
    os._exit(0)


if __name__ == "__main__":
    main()
