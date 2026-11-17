"""订阅内容解析：把三种常见订阅格式统一成 Clash(mihomo) 的 proxy 字典。

支持：
  1. Clash / mihomo 完整配置或 proxies 片段（YAML）
  2. base64 编码的分享链接列表（vmess/vless/trojan/ss/hysteria2 混排）
  3. 明文分享链接列表（一行一条）
"""
from __future__ import annotations

import base64
import ipaddress
import json
import urllib.parse
from typing import Any

import yaml

from .config import SUPPORTED_TYPES

URI_SCHEMES = (
    "vmess://", "vless://", "trojan://", "ss://", "ssr://",
    "hysteria2://", "hy2://", "hysteria://", "tuic://",
    "socks5://", "anytls://",
)

# 免费源里常见的投毒条目：指向回环 / 内网 / 保留地址，永远连不上，
# 只是用来污染别人拉下来的节点池。入库前直接丢掉。
_BAD_HOSTNAMES = {"localhost", "localhost.localdomain", "example.com"}


def _bad_host(host: str) -> bool:
    h = str(host or "").strip().strip("[]").lower()
    if not h or h in _BAD_HOSTNAMES:
        return True
    if len(h) > 253 or any(c in h for c in " \t/\\\"'<>"):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return "." not in h  # 合法主机名至少要有一个点
    return (
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_multicast or ip.is_reserved or ip.is_unspecified
    )


def b64decode_safe(s: str) -> str:
    s = "".join(s.split())
    if not s:
        return ""
    s = s.replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    try:
        return base64.b64decode(s, validate=False).decode("utf-8", "ignore")
    except Exception:
        return ""


def _port(v: Any) -> int:
    try:
        p = int(str(v).strip())
    except Exception:
        return 0
    return p if 0 < p < 65536 else 0


def _truthy(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "tls", "reality")


def _qs(query: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in urllib.parse.parse_qsl(query, keep_blank_values=True):
        out[k.lower()] = v
    return out


def _clean_name(name: Any, server: str, port: int) -> str:
    s = str(name or "").strip()
    s = s.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    if not s:
        s = f"{server}:{port}"
    return s[:48]


def _finalize(p: dict) -> dict | None:
    """统一收尾：校验必备字段、规范化类型。"""
    if not isinstance(p, dict):
        return None
    t = str(p.get("type") or "").strip().lower()
    if t == "hy2":
        t = "hysteria2"
    if t not in SUPPORTED_TYPES:
        return None
    server = str(p.get("server") or "").strip().strip("[]")
    port = _port(p.get("port"))
    if not server or not port or _bad_host(server):
        return None
    p["type"] = t
    p["server"] = server
    p["port"] = port
    p["name"] = _clean_name(p.get("name"), server, port)
    if "udp" not in p:
        p["udp"] = True
    return p


# ---------------- YAML（Clash 格式） ----------------

def _from_yaml(text: str) -> list[dict]:
    try:
        data = yaml.safe_load(text)
    except Exception:
        return []
    proxies: Any = []
    if isinstance(data, dict):
        proxies = data.get("proxies") or data.get("Proxy") or data.get("proxy") or []
    elif isinstance(data, list):
        proxies = data
    if not isinstance(proxies, list):
        return []
    out: list[dict] = []
    for item in proxies:
        if not isinstance(item, dict):
            continue
        p = dict(item)
        # 统一常见的 sni 字段名
        if not p.get("servername") and isinstance(p.get("sni"), str):
            p["servername"] = p["sni"]
        fp = _finalize(p)
        if fp:
            out.append(fp)
    return out


# ---------------- 分享链接 ----------------

def _vmess(uri: str) -> dict | None:
    raw = b64decode_safe(uri[len("vmess://"):])
    if not raw:
        return None
    try:
        j = json.loads(raw)
    except Exception:
        return None
    if not isinstance(j, dict):
        return None

    host = str(j.get("add") or "").strip()
    port = _port(j.get("port"))
    uuid = str(j.get("id") or "").strip()
    if not host or not port or not uuid:
        return None

    p: dict[str, Any] = {
        "name": j.get("ps") or f"{host}:{port}",
        "type": "vmess",
        "server": host,
        "port": port,
        "uuid": uuid,
        "alterId": _port(j.get("aid")) or 0,
        "cipher": str(j.get("scy") or "auto") or "auto",
        "udp": True,
    }
    net = str(j.get("net") or "tcp").lower()
    hdr_host = str(j.get("host") or "").strip()
    path = str(j.get("path") or "").strip()
    sni = str(j.get("sni") or "").strip()
    tls = str(j.get("tls") or "").strip().lower()

    if tls and tls != "none":
        p["tls"] = True
        p["servername"] = sni or hdr_host or host

    if net == "ws":
        p["network"] = "ws"
        p["ws-opts"] = {"path": path or "/"}
        if hdr_host:
            p["ws-opts"]["headers"] = {"Host": hdr_host}
    elif net == "grpc":
        p["network"] = "grpc"
        p["grpc-opts"] = {"grpc-service-name": path or ""}
    elif net in ("h2", "http") and str(j.get("type") or "") != "http":
        p["network"] = "h2"
        p["h2-opts"] = {"path": path or "/"}
        if hdr_host:
            p["h2-opts"]["host"] = [hdr_host]
    return p


def _vless(uri: str) -> dict | None:
    u = urllib.parse.urlsplit(uri)
    uuid = urllib.parse.unquote(u.username or "")
    host = (u.hostname or "").strip()
    port = _port(u.port)
    if not uuid or not host or not port:
        return None
    q = _qs(u.query)
    p: dict[str, Any] = {
        "name": urllib.parse.unquote(u.fragment) or f"{host}:{port}",
        "type": "vless",
        "server": host,
        "port": port,
        "uuid": uuid,
        "udp": True,
    }
    sec = (q.get("security") or "none").lower()
    if sec in ("tls", "reality", "xtls"):
        p["tls"] = True
        p["servername"] = q.get("sni") or q.get("peer") or q.get("host") or host
    if sec == "reality":
        p["reality-opts"] = {}
        if q.get("pbk"):
            p["reality-opts"]["public-key"] = q["pbk"]
        if q.get("sid"):
            p["reality-opts"]["short-id"] = q["sid"]
    if q.get("fp"):
        p["client-fingerprint"] = q["fp"]
    if q.get("flow") and q["flow"] != "none":
        p["flow"] = q["flow"]
    if _truthy(q.get("allowinsecure", "")):
        p["skip-cert-verify"] = True

    net = (q.get("type") or "tcp").lower()
    if net == "ws":
        p["network"] = "ws"
        p["ws-opts"] = {"path": q.get("path") or "/"}
        if q.get("host"):
            p["ws-opts"]["headers"] = {"Host": q["host"]}
    elif net == "grpc":
        p["network"] = "grpc"
        p["grpc-opts"] = {"grpc-service-name": q.get("servicename") or q.get("path") or ""}
    elif net in ("h2", "http"):
        p["network"] = "h2"
        p["h2-opts"] = {"path": q.get("path") or "/"}
        if q.get("host"):
            p["h2-opts"]["host"] = [q["host"]]
    return p


def _trojan(uri: str) -> dict | None:
    u = urllib.parse.urlsplit(uri)
    password = urllib.parse.unquote(u.username or "")
    host = (u.hostname or "").strip()
    port = _port(u.port)
    if not password or not host or not port:
        return None
    q = _qs(u.query)
    p: dict[str, Any] = {
        "name": urllib.parse.unquote(u.fragment) or f"{host}:{port}",
        "type": "trojan",
        "server": host,
        "port": port,
        "password": password,
        "udp": True,
        "sni": q.get("sni") or q.get("peer") or host,
        "skip-cert-verify": True,
    }
    if (q.get("type") or "").lower() == "ws":
        p["network"] = "ws"
        p["ws-opts"] = {"path": q.get("path") or "/"}
        if q.get("host"):
            p["ws-opts"]["headers"] = {"Host": q["host"]}
    elif (q.get("type") or "").lower() == "grpc":
        p["network"] = "grpc"
        p["grpc-opts"] = {"grpc-service-name": q.get("servicename") or ""}
    return p


def _ss(uri: str) -> dict | None:
    body = uri[len("ss://"):]
    frag = ""
    if "#" in body:
        body, frag = body.split("#", 1)
        frag = urllib.parse.unquote(frag)
    if "?" in body:
        body = body.split("?", 1)[0]

    method = password = ""
    hostport = ""
    if "@" in body:
        userinfo, hostport = body.rsplit("@", 1)
        userinfo = urllib.parse.unquote(userinfo)
        if ":" in userinfo:
            method, password = userinfo.split(":", 1)
        else:
            dec = b64decode_safe(userinfo)
            if ":" not in dec:
                return None
            method, password = dec.split(":", 1)
    else:
        dec = b64decode_safe(body)
        if "@" not in dec:
            return None
        userinfo, hostport = dec.rsplit("@", 1)
        if ":" not in userinfo:
            return None
        method, password = userinfo.split(":", 1)

    sp = urllib.parse.urlsplit("//" + hostport)
    host = (sp.hostname or "").strip()
    port = _port(sp.port)
    if not method or not host or not port:
        return None
    return {
        "name": frag or f"{host}:{port}",
        "type": "ss",
        "server": host,
        "port": port,
        "cipher": method.strip().lower(),
        "password": password,
        "udp": True,
    }


def _hysteria2(uri: str) -> dict | None:
    if uri.startswith("hy2://"):
        uri = "hysteria2://" + uri[len("hy2://"):]
    u = urllib.parse.urlsplit(uri)
    host = (u.hostname or "").strip()
    port = _port(u.port)
    password = urllib.parse.unquote(u.username or "")
    if u.password:
        password = f"{password}:{urllib.parse.unquote(u.password)}"
    if not host or not port or not password:
        return None
    q = _qs(u.query)
    p: dict[str, Any] = {
        "name": urllib.parse.unquote(u.fragment) or f"{host}:{port}",
        "type": "hysteria2",
        "server": host,
        "port": port,
        "password": password,
        "sni": q.get("sni") or host,
        "skip-cert-verify": True,
    }
    if q.get("obfs"):
        p["obfs"] = q["obfs"]
        p["obfs-password"] = q.get("obfs-password") or q.get("obfspassword") or ""
    return p


def _tuic(uri: str) -> dict | None:
    u = urllib.parse.urlsplit(uri)
    host = (u.hostname or "").strip()
    port = _port(u.port)
    uuid = urllib.parse.unquote(u.username or "")
    password = urllib.parse.unquote(u.password or "")
    if not host or not port or not uuid:
        return None
    q = _qs(u.query)
    return {
        "name": urllib.parse.unquote(u.fragment) or f"{host}:{port}",
        "type": "tuic",
        "server": host,
        "port": port,
        "uuid": uuid,
        "password": password,
        "sni": q.get("sni") or host,
        "skip-cert-verify": True,
        "udp": True,
    }


def _socks5(uri: str) -> dict | None:
    u = urllib.parse.urlsplit(uri)
    host = (u.hostname or "").strip()
    port = _port(u.port)
    if not host or not port:
        return None
    p: dict[str, Any] = {
        "name": urllib.parse.unquote(u.fragment) or f"{host}:{port}",
        "type": "socks5",
        "server": host,
        "port": port,
        "udp": True,
    }
    if u.username:
        p["username"] = urllib.parse.unquote(u.username)
    if u.password:
        p["password"] = urllib.parse.unquote(u.password)
    return p


_HANDLERS = {
    "vmess://": _vmess,
    "vless://": _vless,
    "trojan://": _trojan,
    "ss://": _ss,
    "hysteria2://": _hysteria2,
    "hy2://": _hysteria2,
    "tuic://": _tuic,
    "socks5://": _socks5,
}


def _from_uris(text: str) -> list[dict]:
    out: list[dict] = []
    for line in text.replace("\r", "\n").split("\n"):
        line = line.strip()
        if not line:
            continue
        for scheme in URI_SCHEMES:
            if line.lower().startswith(scheme):
                handler = _HANDLERS.get(scheme)
                if not handler:
                    break
                try:
                    p = handler(line)
                except Exception:
                    p = None
                if p:
                    fp = _finalize(p)
                    if fp:
                        out.append(fp)
                break
    return out


def parse_subscription(text: str) -> list[dict]:
    """把任意订阅内容解析成 Clash proxy 字典列表。解析不出来返回空列表。"""
    if not text or not text.strip():
        return []
    body = text.strip()

    # 1. 看起来像 Clash YAML
    head = body[:8000]
    if "proxies:" in head or head.lstrip().startswith(("- name:", "proxy-groups:")):
        got = _from_yaml(body)
        if got:
            return got

    # 2. 明文分享链接
    if any(s in body[:4000].lower() for s in ("://",)):
        got = _from_uris(body)
        if got:
            return got

    # 3. 整体 base64（V2Ray 订阅常见）
    dec = b64decode_safe(body)
    if dec:
        if "proxies:" in dec[:8000]:
            got = _from_yaml(dec)
            if got:
                return got
        got = _from_uris(dec)
        if got:
            return got

    # 4. 兜底再试一次 YAML（有些源用 proxies 之外的结构）
    return _from_yaml(body)


def fingerprint(p: dict) -> str:
    """节点指纹：同一条隧道无论从几个源出现都算一个节点。"""
    import hashlib

    cred = ""
    for key in ("uuid", "password", "auth-str", "auth_str"):
        if p.get(key):
            cred = str(p[key])
            break
    if not cred and p.get("cipher"):
        cred = str(p["cipher"])
    raw = "|".join(
        [
            str(p.get("type", "")).lower(),
            str(p.get("server", "")).lower(),
            str(p.get("port", "")),
            cred,
        ]
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()
