"""采集管道：拉源 -> 解析 -> 入库 -> 测速 -> 淘汰。以及后台定时循环。"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import yaml

from . import db, pool, prober
from .config import SOURCES_PATH
from .parsers import parse_subscription

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 ClashNodePool/1.0"
)

_state: dict[str, Any] = {
    "running": False,
    "last_start": 0,
    "last_end": 0,
    "last_trigger": "",
    "last_result": {},
}
_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


def state() -> dict:
    return dict(_state)


# ---------------- 源种子 ----------------

def seed_sources() -> int:
    """首次启动时把 config/sources.yaml 里的源灌进数据库。已存在的不覆盖。"""
    if not SOURCES_PATH.exists():
        return 0
    try:
        data = yaml.safe_load(SOURCES_PATH.read_text(encoding="utf-8")) or {}
    except Exception as e:
        db.log("error", "collect", f"sources.yaml 解析失败：{e}")
        return 0
    items = data.get("sources") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return 0
    added = 0
    for it in items:
        if not isinstance(it, dict) or not it.get("url"):
            continue
        name = str(it.get("name") or it["url"])[:60]
        url = str(it["url"]).strip()
        enabled = 1 if it.get("enabled", True) else 0
        priority = int(it.get("priority", 100) or 100)
        cur = db.execute(
            "INSERT OR IGNORE INTO sources (name, url, enabled, priority) VALUES (?,?,?,?)",
            (name, url, enabled, priority),
        )
        added += cur.rowcount or 0
    if added:
        db.log("info", "collect", f"从 sources.yaml 载入 {added} 个订阅源")
    return added


def list_sources() -> list[dict]:
    rows = db.query("SELECT * FROM sources ORDER BY priority ASC, id ASC")
    out = []
    for r in rows:
        d = dict(r)
        d["alive_rate"] = (
            round(r["fetch_ok"] / r["fetch_total"] * 100, 1) if r["fetch_total"] else None
        )
        out.append(d)
    return out


# ---------------- 拉取 ----------------

async def _fetch_one(client: httpx.AsyncClient, src: dict, timeout: float) -> tuple[dict, str, str]:
    """拉一个源。网络抖动很常见，失败重试一次再放弃。"""
    last_err = ""
    for attempt in range(2):
        try:
            r = await client.get(src["url"], follow_redirects=True)
            if r.status_code != 200:
                last_err = f"HTTP {r.status_code}"
            else:
                text = r.text
                if len(text.strip()) < 10:
                    last_err = "响应为空"
                else:
                    return src, text, ""
        except httpx.TimeoutException:
            last_err = "超时"
        except httpx.HTTPError as e:
            last_err = type(e).__name__
        except Exception as e:
            last_err = type(e).__name__
        if attempt == 0:
            await asyncio.sleep(1.5)
    return src, "", last_err or "未知错误"


async def collect(cfg: dict) -> dict:
    """并发拉取所有启用的源并入库。"""
    sources = [dict(r) for r in db.query(
        "SELECT * FROM sources WHERE enabled=1 ORDER BY priority ASC"
    )]
    if not sources:
        db.log("warn", "collect", "没有启用的订阅源")
        return {"sources": 0, "added": 0, "known": 0, "failed": 0}

    timeout = float(cfg.get("fetch_timeout", 20))
    headers = {"User-Agent": UA, "Accept": "*/*"}
    limits = httpx.Limits(max_connections=24, max_keepalive_connections=12)
    added = known = failed = 0

    async with httpx.AsyncClient(
        timeout=timeout, headers=headers, limits=limits, verify=False
    ) as client:
        results = await asyncio.gather(
            *[_fetch_one(client, s, timeout) for s in sources], return_exceptions=True
        )

    now = int(time.time())
    for item in results:
        if isinstance(item, Exception):
            failed += 1
            continue
        src, text, err = item
        if err:
            failed += 1
            db.execute(
                "UPDATE sources SET last_fetch_ts=?, last_status=?, fetch_total=fetch_total+1 "
                "WHERE id=?",
                (now, err[:80], src["id"]),
            )
            db.log("warn", "collect", f"拉取失败：{err}", src["name"])
            continue
        proxies = parse_subscription(text)
        if not proxies:
            failed += 1
            db.execute(
                "UPDATE sources SET last_fetch_ts=?, last_status=?, fetch_total=fetch_total+1 "
                "WHERE id=?",
                (now, "无可解析节点", src["id"]),
            )
            db.log("warn", "collect", "解析出 0 个节点（源可能已失效或改了格式）", src["name"])
            continue
        a, k = pool.upsert_nodes(proxies, src["name"])
        added += a
        known += k
        db.execute(
            "UPDATE sources SET last_fetch_ts=?, last_status=?, last_node_count=?, "
            "fetch_total=fetch_total+1, fetch_ok=fetch_ok+1 WHERE id=?",
            (now, f"OK +{a}", len(proxies), src["id"]),
        )
        db.log("info", "collect", f"解析 {len(proxies)} 条，新增 {a} 条", src["name"])

    return {"sources": len(sources), "added": added, "known": known, "failed": failed}


# ---------------- 测速 ----------------

async def probe_all(cfg: dict) -> dict:
    items = pool.candidates(
        int(cfg.get("fail_streak_kick", 3)),
        int(cfg.get("probe_limit", 1200)),
    )
    if not items:
        return {"probed": 0, "ok": 0}
    kick = int(cfg.get("fail_streak_kick", 3))

    def on_result(fp: str, ok: bool, ms, err: str) -> None:
        pool.record_result(fp, ok, ms, err, kick)

    res = await prober.probe_batch(
        items,
        concurrency=int(cfg.get("probe_concurrency", 128)),
        timeout=float(cfg.get("probe_timeout", 4.0)),
        on_result=on_result,
    )
    ok = sum(1 for v in res.values() if v[0])
    db.log("info", "probe", f"探测 {len(items)} 条，存活 {ok} 条")
    return {"probed": len(items), "ok": ok}


# ---------------- 一轮完整流程 ----------------

async def run_once(trigger: str = "auto") -> dict:
    st = _state
    async with _get_lock():
        if st["running"]:
            return {"skipped": True, "reason": "running"}
        st["running"] = True
        st["last_start"] = int(time.time())
        st["last_trigger"] = trigger
        cfg = db.get_settings()
        t0 = time.perf_counter()
        try:
            db.log("info", "collect", f"开始一轮刷新（{trigger}）")
            c = await collect(cfg)
            revived = pool.requalify()
            p = await probe_all(cfg)
            pr = pool.prune(cfg)
            s = pool.stats()
            result = {
                "collect": c,
                "revived": revived,
                "probe": p,
                "prune": pr,
                "stats": s,
                "elapsed_sec": round(time.perf_counter() - t0, 1),
            }
            db.log(
                "info",
                "prune",
                f"本轮完成：新增 {c.get('added', 0)}，探测 {p.get('probed', 0)}／活 "
                f"{p.get('ok', 0)}，踢出 {pr['kicked_dead'] + pr['kicked_expired'] + pr['kicked_overflow']}，"
                f"池内 {s['alive']} 条",
            )
        except Exception as e:
            db.log("error", "collect", f"本轮异常：{type(e).__name__}: {e}")
            result = {"error": f"{type(e).__name__}: {e}"}
        finally:
            st["running"] = False
            st["last_end"] = int(time.time())
            st["last_result"] = result
            db.trim_logs(2000)
        return result


def trigger(trigger_name: str = "manual") -> bool:
    """异步触发一轮刷新，不阻塞请求。返回是否成功启动。"""
    if _state["running"]:
        return False

    async def _run():
        await run_once(trigger_name)

    try:
        asyncio.create_task(_run())
        return True
    except RuntimeError:
        return False


async def scheduler_loop() -> None:
    """后台定时循环。首轮延迟 5 秒启动，避免和界面加载抢资源。"""
    await asyncio.sleep(5)
    while True:
        cfg = db.get_settings()
        try:
            if cfg.get("auto_refresh", True):
                await run_once("auto")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            db.log("error", "collect", f"调度异常：{type(e).__name__}: {e}")
        cfg = db.get_settings()
        interval = max(120, int(cfg.get("interval_sec", 1800)))
        await asyncio.sleep(interval)
