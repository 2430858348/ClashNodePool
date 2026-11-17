"""全局配置与路径常量。"""
from __future__ import annotations

from pathlib import Path

APP_NAME = "ClashNodePool"
APP_TITLE = "ClashNodePool · 节点池订阅"
APP_DESC = "本地运行的 Clash 节点池服务"

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
DATA_DIR = BASE_DIR / "data"
WEB_DIR = BASE_DIR / "web"
DB_PATH = DATA_DIR / "nodes.db"
SOURCES_PATH = CONFIG_DIR / "sources.yaml"

HOST = "127.0.0.1"
PORT = 8787

# 可在 config/settings.json 中覆盖任意一项
DEFAULT_SETTINGS: dict = {
    # 后台自动刷新间隔（秒）
    "interval_sec": 1800,
    # 测速并发数
    "probe_concurrency": 128,
    # 单轮最多测多少个节点（超出的下一轮再测，按最久未测优先轮转）
    "probe_limit": 1200,
    # 单个节点测速超时（秒）
    "probe_timeout": 4.0,
    # 拉取单个订阅源的超时（秒）
    "fetch_timeout": 30.0,
    # 节点池输出上限（按延迟取前 N）
    "max_nodes": 200,
    # 节点最长存活时间（小时），超过即踢出，防止池子里堆积僵尸
    "node_ttl_hours": 72,
    # 连续失败多少次踢出
    "fail_streak_kick": 3,
    # 新建订阅的默认有效期（小时）
    "default_token_ttl_hours": 168,
    # 自动刷新是否启用
    "auto_refresh": True,
    # 关闭网页后自动停止服务（仅通过桌面图标启动时生效）
    "auto_stop_on_page_close": True,
    # 心跳超时兜底（秒）：后台标签页会被浏览器限流到约 1 次/分钟，放宽到这里不会误杀
    "heartbeat_timeout_sec": 150,
    # 收到「页面关闭」后的宽限时间（秒），用于容错刷新
    "shutdown_grace_sec": 10,
}

# 基于 UDP/QUIC 的协议，TCP 探测对其无效，不参与连通性判定
UDP_PROTOCOLS = {"hysteria2", "hysteria", "tuic", "wireguard"}

SUPPORTED_TYPES = {
    "vmess", "vless", "trojan", "ss", "ssr",
    "hysteria2", "hysteria", "tuic", "socks5", "http", "anytls",
}
