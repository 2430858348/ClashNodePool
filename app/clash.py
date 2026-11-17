"""把节点池输出成 mihomo(Clash Meta) 可直接加载的完整配置。

所有字符串都由 yaml.safe_dump 序列化，节点名里的冒号、括号、引号不会破坏 YAML。
"""
from __future__ import annotations

from typing import Iterable

import yaml

TEST_URL = "http://www.gstatic.com/generate_204"
GROUP_AUTO = "自动选择"
GROUP_MANUAL = "手动选择"

# 白名单：只输出 mihomo 认得的字段。免费源里常夹带自定义字段，
# 未知字段有可能让整份配置加载失败，那会把用户现有的 Clash 直接搞崩。
COMMON_KEYS = {
    "name", "type", "server", "port", "udp", "tfo", "mptcp", "uot",
    "ip-version", "interface-name", "routing-mark", "dialer-proxy", "smux",
}

TYPE_KEYS: dict[str, set] = {
    "vmess": {
        "uuid", "alterId", "cipher", "tls", "servername", "network",
        "ws-opts", "grpc-opts", "h2-opts", "http-opts", "reality-opts",
        "skip-cert-verify", "client-fingerprint", "fingerprint",
        "packet-encoding", "global-padding", "authenticated-length",
    },
    "vless": {
        "uuid", "flow", "tls", "servername", "network", "ws-opts",
        "grpc-opts", "h2-opts", "http-opts", "reality-opts",
        "skip-cert-verify", "client-fingerprint", "fingerprint",
        "packet-encoding",
    },
    "trojan": {
        "password", "sni", "alpn", "skip-cert-verify", "network", "ws-opts",
        "grpc-opts", "fingerprint", "client-fingerprint", "flow", "reality-opts",
    },
    "ss": {
        "cipher", "password", "plugin", "plugin-opts", "udp-over-tcp",
        "udp-over-tcp-version", "client-fingerprint",
    },
    "ssr": {"cipher", "password", "protocol", "protocol-param", "obfs", "obfs-param"},
    "hysteria2": {
        "password", "obfs", "obfs-password", "sni", "alpn",
        "skip-cert-verify", "fingerprint", "up", "down", "ports", "hop-interval",
    },
    "hysteria": {
        "auth-str", "auth_str", "obfs", "alpn", "protocol", "up", "down",
        "sni", "skip-cert-verify", "fingerprint", "ports", "recv-window-conn",
        "recv-window", "disable-mtu-discovery",
    },
    "tuic": {
        "uuid", "password", "sni", "alpn", "skip-cert-verify",
        "congestion-controller", "udp-relay-mode", "reduce-rtt",
        "heartbeat-interval", "disable-sni", "fingerprint",
    },
    "anytls": {
        "password", "sni", "skip-cert-verify", "client-fingerprint", "alpn",
        "idle-session-check-interval", "idle-session-timeout", "min-idle-session",
    },
    "socks5": {"username", "password", "tls", "skip-cert-verify"},
    "http": {"username", "password", "tls", "sni", "skip-cert-verify", "headers"},
}


def sanitize_proxy(raw: dict) -> dict:
    """按类型白名单裁剪字段，并统一几个大小写不一致的键名。"""
    t = str(raw.get("type") or "").strip().lower()
    if t == "hy2":
        t = "hysteria2"
    if "alterid" in raw and "alterId" not in raw:
        raw["alterId"] = raw.pop("alterid")
    if "auth_str" in raw and "auth-str" not in raw:
        raw["auth-str"] = raw.pop("auth_str")
    allowed = COMMON_KEYS | TYPE_KEYS.get(t, set())
    out = {k: v for k, v in raw.items() if k in allowed}
    out["type"] = t
    return out

RULES = [
    "DOMAIN-SUFFIX,local,DIRECT",
    "IP-CIDR,127.0.0.0/8,DIRECT,no-resolve",
    "IP-CIDR,10.0.0.0/8,DIRECT,no-resolve",
    "IP-CIDR,172.16.0.0/12,DIRECT,no-resolve",
    "IP-CIDR,192.168.0.0/16,DIRECT,no-resolve",
    "GEOIP,CN,DIRECT",
    f"MATCH,{GROUP_AUTO}",
]


def build_dict(nodes: Iterable[dict], cfg: dict | None = None) -> dict:
    """组装成 Clash 配置字典（不依赖 YAML 库，便于单元测试）。"""
    cfg = cfg or {}
    proxies: list[dict] = []
    names: list[str] = []
    for item in nodes:
        raw = sanitize_proxy(dict(item["raw"]))
        name = item["name"]
        proxies.append({"name": name, **raw})
        names.append(name)

    if not names:
        # 空池子也不能返回空 proxies，否则 Clash 会清空你现有节点。
        # 这里给一条 DIRECT 以便配置仍然合法，但订阅接口在池子为空时会优先返回 503。
        groups = []
        rules = ["MATCH,DIRECT"]
    else:
        groups = [
            {
                "name": GROUP_AUTO,
                "type": "url-test",
                "url": TEST_URL,
                "interval": 300,
                "tolerance": 50,
                "lazy": True,
                "proxies": names,
            },
            {
                "name": GROUP_MANUAL,
                "type": "select",
                "proxies": [GROUP_AUTO, *names],
            },
        ]
        rules = RULES

    config: dict = {
        "mixed-port": 7890,
        "allow-lan": False,
        "bind-address": "*",
        "mode": "rule",
        "log-level": "info",
        "ipv6": False,
        "external-controller": "",
        "dns": {
            "enable": True,
            "ipv6": False,
            "enhanced-mode": "redir-host",
            "nameserver": ["223.5.5.5", "119.29.29.29", "114.114.114.114"],
        },
    }
    if proxies:
        config["proxies"] = proxies
        config["proxy-groups"] = groups
    elif groups:
        config["proxy-groups"] = groups
    config["rules"] = rules
    return config


def build_config(nodes: Iterable[dict], cfg: dict | None = None) -> str:
    return yaml.safe_dump(
        build_dict(nodes, cfg),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=100000,
    )


def subscription_headers(token_expires: int) -> dict:
    return {
        "Content-Type": "text/yaml; charset=utf-8",
        "Content-Disposition": 'attachment; filename="nodes.yaml"',
        "Profile-Update-Interval": "6",
        "Cache-Control": "no-store",
        "Subscription-Expires": str(token_expires),
        "Access-Control-Allow-Origin": "*",
    }
