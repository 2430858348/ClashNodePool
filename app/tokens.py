"""订阅令牌：签发带有效期的订阅链接，并负责校验与生命周期管理。"""
from __future__ import annotations

import secrets
import time

from . import db

TOKEN_BYTES = 18  # url-safe 之后约 24 字符


class TokenError(Exception):
    """令牌不可用的统一异常，message 直接给用户看。"""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


def create(label: str, ttl_hours: float) -> dict:
    now = int(time.time())
    expires = now + int(max(0.0167, ttl_hours) * 3600)
    token = secrets.token_urlsafe(TOKEN_BYTES)
    db.execute(
        "INSERT INTO tokens (token, label, created_ts, expires_ts, revoked) VALUES (?,?,?,?,0)",
        (token, (label or "").strip()[:60], now, expires),
    )
    db.log("info", "token", f"签发订阅令牌 {token[:8]}…，有效期至 {expires}")
    return get(token)


def _decorate(d: dict) -> dict:
    now = int(time.time())
    d["remaining_sec"] = max(0, d["expires_ts"] - now)
    d["expired"] = d["expires_ts"] <= now
    d["state"] = "revoked" if d["revoked"] else ("expired" if d["expired"] else "active")
    return d


def _row(token: str):
    return db.query_one("SELECT * FROM tokens WHERE token=?", (token,))


def get(token: str) -> dict | None:
    r = _row(token)
    return _decorate(dict(r)) if r else None


def list_all() -> list[dict]:
    rows = db.query("SELECT * FROM tokens ORDER BY created_ts DESC")
    return [_decorate(dict(r)) for r in rows]


def verify(token: str) -> dict:
    """校验令牌，不通过直接抛 TokenError。通过则刷新拉取统计并返回记录。"""
    r = _row(token)
    if r is None:
        raise TokenError("not_found", "订阅不存在")
    if r["revoked"]:
        raise TokenError("revoked", "订阅已被吊销")
    now = int(time.time())
    if r["expires_ts"] <= now:
        raise TokenError(
            "expired",
            f"订阅已于 {time.strftime('%Y-%m-%d %H:%M', time.localtime(r['expires_ts']))} 过期",
        )
    db.execute(
        "UPDATE tokens SET fetch_count=fetch_count+1, last_fetch_ts=? WHERE token=?",
        (now, token),
    )
    return dict(r)


def extend(token: str, hours: float = 0, extend_from_now: bool = False) -> dict:
    r = _row(token)
    if r is None:
        raise TokenError("not_found", "订阅不存在")
    now = int(time.time())
    base = now if extend_from_now else max(r["expires_ts"], now)
    new_exp = base + int(max(0, hours) * 3600)
    db.execute("UPDATE tokens SET expires_ts=? WHERE token=?", (new_exp, token))
    db.log("info", "token", f"续期 {token[:8]}… 至 {new_exp}")
    return get(token)  # type: ignore[return-value]


def revoke(token: str, revoked: bool = True) -> dict:
    if _row(token) is None:
        raise TokenError("not_found", "订阅不存在")
    db.execute("UPDATE tokens SET revoked=? WHERE token=?", (1 if revoked else 0, token))
    db.log("warn", "token", f"{'吊销' if revoked else '恢复'}订阅 {token[:8]}…")
    return get(token)  # type: ignore[return-value]


def delete(token: str) -> None:
    db.execute("DELETE FROM tokens WHERE token=?", (token,))
    db.log("warn", "token", f"删除订阅 {token[:8]}…")


def touch_reject(token: str, reason: str) -> None:
    """记录一次被拒绝的拉取，方便在日志里排查「为什么 Clash 更新失败」。"""
    db.log("warn", "sub", f"拒绝拉取 {token[:8]}…（{reason}）")
