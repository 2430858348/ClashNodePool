"""节点池管理：入库去重、结果回写、TTL 与失败连击淘汰、按延迟取前 N。"""
from __future__ import annotations

import json
import time
from typing import Iterable

from . import db
from .config import UDP_PROTOCOLS
from .parsers import fingerprint

CHUNK = 500          # SQLite 变量上限保守值
REQUALIFY_COOLDOWN = 6 * 3600   # 被判死的节点隔多久允许重新试一次


def requalify() -> int:
    """冷却期过后清零失败计数，让被判死的节点重新进入候选（探测通过才会重新入池）。"""
    now = int(time.time())
    cur = db.execute(
        "UPDATE nodes SET fail_streak=0 "
        "WHERE alive=0 AND last_check_ts > 0 AND last_check_ts < ?",
        (now - REQUALIFY_COOLDOWN,),
    )
    return cur.rowcount or 0


def upsert_nodes(proxies: Iterable[dict], source_name: str) -> tuple[int, int]:
    """写入节点，返回 (新增数, 已知数)。已存在的只更新名称与来源，不动首见时间。"""
    now = int(time.time())
    rows: list[tuple] = []
    seen: set[str] = set()
    for p in proxies:
        fp = fingerprint(p)
        if not fp or fp in seen:
            continue
        seen.add(fp)
        # UDP 系协议无法用 TCP 验证，插入即视为可用，由 prune 的配额限制数量；
        # 其余协议先记为不可用，等探测通过再放行。
        udp = 1 if str(p.get("type", "")).lower() in UDP_PROTOCOLS else 0
        rows.append(
            (
                fp,
                p["name"],
                p["type"],
                p["server"],
                int(p["port"]),
                json.dumps(p, ensure_ascii=False),
                source_name,
                now,
                udp,
                udp,
            )
        )
    if not rows:
        return 0, 0

    fps = [r[0] for r in rows]
    existing: set[str] = set()
    for i in range(0, len(fps), CHUNK):
        part = fps[i:i + CHUNK]
        sql = "SELECT fp FROM nodes WHERE fp IN (" + ",".join("?" * len(part)) + ")"
        existing.update(r["fp"] for r in db.query(sql, part))

    fresh = [r for r in rows if r[0] not in existing]
    known = [r for r in rows if r[0] in existing]

    if fresh:
        db.execute_many(
            "INSERT OR IGNORE INTO nodes "
            "(fp,name,type,server,port,raw,source_name,first_seen,udp_only,alive,verified,fail_streak) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,0,0)",
            fresh,
        )
    if known:
        # UDP 系协议无法验证，每轮重新放行（数量由 prune 的配额控制）
        db.execute_many(
            "UPDATE nodes SET name=?, raw=?, source_name=?, "
            "alive = CASE WHEN udp_only=1 AND verified=0 THEN 1 ELSE alive END "
            "WHERE fp=?",
            [(r[1], r[5], r[6], r[0]) for r in known],
        )
    return len(fresh), len(known)


def record_result(fp: str, ok: bool, ms: int | None, err: str, kick_after: int = 3) -> None:
    """只负责记录探测结果。是否判死统一交给 prune()，保证淘汰统计真实可读。"""
    now = int(time.time())
    if ok:
        db.execute(
            "UPDATE nodes SET last_ok_ts=?, last_check_ts=?, latency_ms=?, "
            "fail_streak=0, alive=1, verified=1 WHERE fp=?",
            (now, now, ms, fp),
        )
    else:
        db.execute(
            "UPDATE nodes SET last_check_ts=?, fail_streak=fail_streak+1 WHERE fp=?",
            (now, fp),
        )


def candidates(kick_after: int = 3, limit: int = 1200) -> list[tuple[str, dict]]:
    """待测节点：从未测过的全部纳入，已验证的复测，已判死的排除，UDP 系协议跳过。

    按「最久没测过」优先，单轮限量，避免一次抓回几千个节点把网络打满；
    没轮到的下一轮自然轮到，池子会逐步被覆盖。
    """
    marks = ",".join("?" * len(UDP_PROTOCOLS)) or "''"
    rows = db.query(
        "SELECT fp, raw FROM nodes WHERE fail_streak < ? "
        "AND type NOT IN (" + marks + ") "
        "ORDER BY last_check_ts ASC, fail_streak ASC LIMIT ?",
        (kick_after, *UDP_PROTOCOLS, max(1, int(limit))),
    )
    out: list[tuple[str, dict]] = []
    for r in rows:
        try:
            out.append((r["fp"], json.loads(r["raw"])))
        except Exception:
            continue
    return out


def prune(cfg: dict) -> dict:
    """淘汰：连续失败 / 存活超 TTL / 超出池容量。"""
    now = int(time.time())
    kick_after = int(cfg.get("fail_streak_kick", 3))
    ttl_hours = float(cfg.get("node_ttl_hours", 72))
    max_nodes = int(cfg.get("max_nodes", 200))

    cur = db.execute(
        "UPDATE nodes SET alive=0 WHERE alive=1 AND fail_streak >= ?", (kick_after,)
    )
    kicked_dead = cur.rowcount or 0

    # TTL 到期直接删行：这样下一轮采集会把它当成新节点重新发现，
    # 而不是留一条永久判死的记录把池子越缩越小。
    kicked_expired = 0
    if ttl_hours > 0:
        cutoff = now - int(ttl_hours * 3600)
        cur = db.execute(
            "DELETE FROM nodes WHERE first_seen > 0 AND first_seen < ?", (cutoff,)
        )
        kicked_expired = cur.rowcount or 0

    kicked_overflow = 0

    # UDP 系协议没法用 TCP 验证，只能限量放进池子，否则会淹没已验证的节点
    udp_cap = max(8, max_nodes // 5)
    udp_rows = db.query(
        "SELECT fp FROM nodes WHERE alive=1 AND udp_only=1 ORDER BY first_seen ASC"
    )
    if len(udp_rows) > udp_cap:
        extra = [r["fp"] for r in udp_rows[udp_cap:]]
        for i in range(0, len(extra), CHUNK):
            part = extra[i:i + CHUNK]
            db.execute(
                "UPDATE nodes SET alive=0 WHERE fp IN (" + ",".join("?" * len(part)) + ")",
                part,
            )
        kicked_overflow += len(extra)
        udp_rows = udp_rows[:udp_cap]

    # 已验证的 TCP 节点按延迟取前 N，超过部分淘汰掉最慢的
    tcp_room = max(1, max_nodes - len(udp_rows))
    tcp_row = db.query_one(
        "SELECT COUNT(*) AS c FROM nodes WHERE alive=1 AND latency_ms IS NOT NULL"
    )
    if tcp_row and tcp_row["c"] > tcp_room:
        # 用「保留前 N 名」的方式淘汰，避免大量同延迟时超额
        cur = db.execute(
            "UPDATE nodes SET alive=0 WHERE alive=1 AND latency_ms IS NOT NULL AND fp NOT IN ("
            "  SELECT fp FROM nodes WHERE alive=1 AND latency_ms IS NOT NULL"
            "  ORDER BY latency_ms ASC, fp ASC LIMIT ?)",
            (tcp_room,),
        )
        kicked_overflow += cur.rowcount or 0

    final_row = db.query_one("SELECT COUNT(*) AS c FROM nodes WHERE alive=1")
    return {
        "kicked_dead": kicked_dead,
        "kicked_expired": kicked_expired,
        "kicked_overflow": kicked_overflow,
        "alive": final_row["c"] if final_row else 0,
    }


def alive_nodes(limit: int) -> list[dict]:
    rows = db.query(
        "SELECT * FROM nodes WHERE alive=1 "
        "ORDER BY (latency_ms IS NULL), latency_ms ASC, first_seen DESC LIMIT ?",
        (limit,),
    )
    out: list[dict] = []
    seen_names: set[str] = set()
    for r in rows:
        try:
            raw = json.loads(r["raw"])
        except Exception:
            continue
        name = str(raw.get("name") or r["name"] or "").strip() or f"{r['server']}:{r['port']}"
        base = name
        n = 2
        while name in seen_names:
            name = f"{base} #{n}"
            n += 1
        seen_names.add(name)
        raw["name"] = name
        out.append(
            {
                "fp": r["fp"],
                "raw": raw,
                "name": name,
                "type": r["type"],
                "server": r["server"],
                "port": r["port"],
                "latency_ms": r["latency_ms"],
                "source_name": r["source_name"],
                "first_seen": r["first_seen"],
                "last_ok_ts": r["last_ok_ts"],
                "udp_only": bool(r["udp_only"]),
            }
        )
    return out


def stats() -> dict:
    now = int(time.time())
    total = db.query_one("SELECT COUNT(*) AS c FROM nodes")
    alive = db.query_one("SELECT COUNT(*) AS c FROM nodes WHERE alive=1")
    udp = db.query_one("SELECT COUNT(*) AS c FROM nodes WHERE alive=1 AND udp_only=1")
    pending = db.query_one("SELECT COUNT(*) AS c FROM nodes WHERE verified=0")
    fresh = db.query_one(
        "SELECT COUNT(*) AS c FROM nodes WHERE alive=1 AND first_seen > ?", (now - 3600,)
    )
    return {
        "total": total["c"] if total else 0,
        "alive": alive["c"] if alive else 0,
        "alive_udp": udp["c"] if udp else 0,
        "pending": pending["c"] if pending else 0,
        "new_last_hour": fresh["c"] if fresh else 0,
    }
