"""连通性探测。

判定分两级：
  1. TCP 三次握手成功  -> 服务器在线、端口开放
  2. TLS 节点再做一次握手 -> 证书层也能走通

注意：UDP 系协议（hysteria2 / tuic）用 TCP 探测必然失败，这里直接跳过，
      标记为 udp_only，不参与存活判定，避免把好节点误杀。
"""
from __future__ import annotations

import asyncio
import socket
import ssl
import time
from typing import Iterable

from .config import UDP_PROTOCOLS

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE
try:
    _SSL_CTX.set_ciphers("DEFAULT@SECLEVEL=1")
except Exception:
    pass


def needs_tls(p: dict) -> bool:
    if p.get("tls") is True or str(p.get("tls")).lower() == "true":
        return True
    return str(p.get("type", "")).lower() in ("trojan", "hysteria2", "hysteria", "tuic", "anytls")


async def probe_one(p: dict, timeout: float) -> tuple[bool, int | None, str]:
    """返回 (是否存活, 延迟毫秒, 错误类型)。"""
    host = str(p.get("server", "")).strip()
    try:
        port = int(p.get("port", 0))
    except Exception:
        return False, None, "bad_port"
    if not host or not port:
        return False, None, "bad_target"

    use_tls = needs_tls(p)
    sni = p.get("servername") or p.get("sni") or host
    t0 = time.perf_counter()
    writer = None
    try:
        fut = asyncio.open_connection(
            host=host,
            port=port,
            ssl=_SSL_CTX if use_tls else None,
            server_hostname=str(sni) if use_tls else None,
            family=socket.AF_UNSPEC,
        )
        reader, writer = await asyncio.wait_for(fut, timeout=timeout)
        ms = int((time.perf_counter() - t0) * 1000)
        return True, ms, ""
    except asyncio.TimeoutError:
        return False, None, "timeout"
    except ssl.SSLError:
        # TLS 握手失败但 TCP 可达，仍算节点服务器活着，只是证书层异常
        ms = int((time.perf_counter() - t0) * 1000)
        return True, ms, "tls_warn"
    except (ConnectionRefusedError,):
        return False, None, "refused"
    except (socket.gaierror,):
        return False, None, "dns"
    except (OSError, asyncio.IncompleteReadError, EOFError):
        return False, None, "network"
    except Exception as e:
        return False, None, type(e).__name__
    finally:
        if writer is not None:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass


async def probe_batch(
    proxies: Iterable[dict],
    concurrency: int = 128,
    timeout: float = 4.0,
    on_result=None,
) -> dict[str, tuple[bool, int | None, str]]:
    """并发探测，返回 {fp: (ok, ms, err)}。on_result 为可选回调，用于边测边落库。"""
    sem = asyncio.Semaphore(max(1, concurrency))
    results: dict[str, tuple[bool, int | None, str]] = {}

    async def worker(fp: str, proxy: dict):
        async with sem:
            ok, ms, err = await probe_one(proxy, timeout)
        results[fp] = (ok, ms, err)
        if on_result is not None:
            try:
                on_result(fp, ok, ms, err)
            except Exception:
                pass

    tasks = [asyncio.create_task(worker(fp, px)) for fp, px in proxies]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    return results


def is_udp_only(p: dict) -> bool:
    return str(p.get("type", "")).lower() in UDP_PROTOCOLS
