"""后台诊断插件 path_doctor v2.5.3

一条命令看清 AstrBot 后台的四个层面：插件 / 框架 / 协议端 / 宿主机。
同时提供「群成员信息导出」（参考 astrbot_plugin_group_information 的能力，
但改为落共享目录 + file:// 发送，适配本机 cloud 环境的文件链路）。

命令（均以 / 开头）：
  /diag                 体检总览：四层结论 + 问题清单
  /diag help [子命令]     命令表；带子命令时看单个命令的用法与示例
  /diag startup          启动/上线自检清单（含共享目录与网络；②③ 与 shared/net 同源）
  /diag plugin [名字|all] 插件逐个体检（目录 / 元数据 / 依赖 / 配置 / 加载状态）
  /diag astrbot          框架体检（版本 / 运行时长 / 平台 / 事件循环 / 库表 / 临时目录）
  /diag proto [群号]      协议端实测（OneBot API 往返 + 端口连接 + 可见性）
  /diag host             宿主机（负载 / 内存 / 磁盘 / inode / 挂载来源 / 压力）
  /diag net              网络实测（DNS/TCP/HTTPS + 代理平面探测与出网实测 → 国内/海外/代理三结论）
  /diag shared [dir]     共享目录对端可见性（协议端能否读到产物）
  /diag writetest [dir]  写入实测（含 umask 影响）
  /diag groups           机器人所在群列表
  /diag export [群号]     导出群成员到 Excel（当前群或指定群）
  /diag exportall        导出全部群成员到多 sheet Excel（管理员）
  /diag report           完整体检报告（7 页 Excel）
  /diag help             帮助

  兼容旧写法：path→总览、perm→shared、write→writetest、txt→report
  自检清单别名：check / 自检 / 启动自检 / 上线自检
  群信息命令别名：群信息 / 导出群数据 / 导出所有群数据

设计取向：
- 只读为主；唯一副作用是写探针/导出文件，且都落在共享目录、自动清理。
- 无第三方依赖（openpyxl 可选，缺失时自动降级为 CSV）。
- 任何单节的异常都不影响其它节：统一 try 包裹，报告里显示 <ERR ...>。
- 代理相关一律只报「端口是否在监听 / 能否出网 / API 是否需鉴权」，绝不读取或打印密钥。
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import re
import socket
import sqlite3
import ssl
import stat as _stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import unicodedata
from datetime import datetime
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import File, Plain
from astrbot.api.star import Context, Star, StarTools, register

PLUGIN_NAME = "path_doctor"
PLUGIN_VERSION = "v2.5.3"

_PROBE_DIR = "path_doctor_probe"
_EXPORT_DIR = "path_doctor_export"
_TEXT_LIMIT = 2800
_META_NAMES = ("metadata.yaml", "metadata.yml")

# ---------------- 网络 / 代理探测常量（v2.5.0） ----------------
# 本机常见代理监听端口（顺序即探测顺序）：mihomo/clash 混合口、clash socks 口、通用 socks 口
_PROXY_PORTS = (
    (7890, "clash/mihomo 混合口"),
    (7891, "clash socks 口"),
    (1080, "通用 socks 口"),
)
# 代理内核进程名（容器内 /proc 扫描用，不依赖 ps）
_PROXY_KERNELS = ("mihomo", "clash", "clash-meta", "xray", "v2ray", "sing-box",
                  "hysteria", "tuic", "naive")
# 代理出网实测目标：204 无正文，最省流量也最能说明「出口真的通」
_PROXY_TEST_URL = "https://www.gstatic.com/generate_204"
_PROXY_API_PORT = 9090

# ---------------- /diag help <子命令> 的单命令详情（v2.5.1） ----------------
# canonical → (用法, 一句话作用, 示例, 相关命令)
_DIAG_DETAIL = {
    "": ("/diag", "体检总览：插件 / AstrBot / 协议端 / 宿主机 四层结论 + 问题清单",
         "/diag", "/diag startup · /diag plugin · /diag report"),
    "startup": ("/diag startup", "启动自检清单：6 项一次看完，② 共享目录与 ③ 网络与对应子命令同源",
                "/diag startup", "/diag shared · /diag net · /diag writetest"),
    "plugin": ("/diag plugin [名字|all]", "插件逐个体检：加载状态 / 元数据 / 依赖 / 配置 / 版本漂移 / 语法",
               "/diag plugin path_doctor", "/diag commands · /diag log"),
    "astrbot": ("/diag astrbot", "框架体检：版本 / 运行时长 / 事件循环延迟 / 平台适配器 / 库表 / 日志",
                "/diag astrbot", "/diag proto · /diag host"),
    "proto": ("/diag proto [群号]", "协议端实测：OneBot API 往返耗时 + 适配器端口连接数",
              "/diag proto 900001", "/diag caps · /diag groups"),
    "host": ("/diag host", "宿主机：负载 / 内存 / Swap / 磁盘 / inode / 挂载来源 / PSI（挂了 docker.sock 还能列容器）",
             "/diag host", "/diag astrbot"),
    "net": ("/diag net", "网络实测：DNS / TCP / HTTPS + 代理四平面探测与出网实测 → 国内 / 海外 / 代理三结论",
            "/diag net", "/diag startup · /diag proto"),
    "shared": ("/diag shared [目录]", "共享目录对端可见性：逐级 others 权限 + 探针 777/666（发文件前必看）",
               "/diag shared", "/diag writetest · /diag startup"),
    "writetest": ("/diag writetest [目录]", "写入实测：能不能写、umask、新建文件的实际权限",
                  "/diag writetest", "/diag shared"),
    "groups": ("/diag groups", "机器人所在群列表（按人数排序、标出本群）",
               "/diag groups", "/diag export · /diag exportall"),
    "export": ("/diag export [群号] [xlsx|json|csv]", "导出某群成员（默认带统计样式的 xlsx）",
               "/diag export 900001 csv", "/diag exportall · /diag groups"),
    "exportall": ("/diag exportall [xlsx|json|csv]", "导出全部群成员（多 sheet xlsx，管理员）",
                  "/diag exportall", "/diag export"),
    "report": ("/diag report", "体检报告：7 页 Excel（总览/插件/框架/协议端/宿主机/网络/文件链路）",
               "/diag report", "/diag txt · /diag pack · /diag json"),
    "txt": ("/diag txt", "体检报告纯文本版（排查用，落共享目录后发出）",
            "/diag txt", "/diag report · /diag md"),
    "json": ("/diag json", "结构化快照 JSON（给别的插件 / 脚本读，含 net 与协议端探测）",
             "/diag json", "/diag md · /diag caps · /diag pack"),
    "md": ("/diag md", "Markdown 报告（可直接贴文档：概览/插件/平台/协议端/网络/路径）",
           "/diag md", "/diag json · /diag report"),
    "caps": ("/diag caps", "协议端能力矩阵：逐个只读 API 实测支持情况（✅/⚠️/❌/⏱ + 耗时）",
             "/diag caps", "/diag proto"),
    "pack": ("/diag pack", "一键归档 zip：体检报告.xlsx + snapshot.json + caps.json + manifest.json",
             "/diag pack", "/diag report · /diag json · /diag caps"),
    "log": ("/diag log [n]", "日志按插件聚合 ERROR/WARN（需先开 log_file_enable）",
            "/diag log 50", "/diag plugin"),
    "commands": ("/diag commands", "跨插件命令重名扫描（静态检查）",
                 "/diag commands", "/diag plugin"),
    "snapshot": ("/diag snapshot", "写一份自检快照到 plugin_data（auto_snapshot 开启时还会定时写）",
                 "/diag snapshot", "/diag json · /diag report"),
    "help": ("/diag help [子命令]", "命令表；带子命令时看单个命令的用法与示例",
             "/diag help net", "/diag startup"),
}
# 别名 → canonical（与命令分派保持一致；只用于 help 解析）
_DIAG_ALIASES = {
    "path": "", "overview": "", "all": "",
    "plugins": "plugin", "star": "plugin",
    "framework": "astrbot", "core": "astrbot",
    "snowluma": "proto", "onebot": "proto", "qq": "proto", "protocol": "proto",
    "sys": "host", "system": "host", "machine": "host",
    "network": "net", "proxy": "net",
    "perm": "shared", "share": "shared",
    "write": "writetest", "w": "writetest",
    "check": "startup", "自检": "startup", "启动自检": "startup", "上线自检": "startup",
    "grouplist": "groups", "群列表": "groups",
    "导出": "export", "导出所有": "exportall", "export_all": "exportall",
    "xlsx": "report", "excel": "report",
    "snapshot_json": "json",
    "markdown": "md",
    "capability": "caps", "capabilities": "caps", "matrix": "caps",
    "bundle": "pack", "zip": "pack",
    "logs": "log",
    "cmd": "commands", "conflicts": "commands",
    "snap": "snapshot",
    "?": "help", "？": "help", "帮助": "help",
}


# ============================ 通用工具 ============================
def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _tz_note() -> str:
    try:
        now = datetime.now().astimezone()
        off = now.utcoffset()
        total = int(off.total_seconds()) // 60 if off else 0
        sign = "+" if total >= 0 else "-"
        total = abs(total)
        name = now.tzname() or ""
        return f"UTC{sign}{total // 60:02d}:{total % 60:02d}{(' ' + name) if name else ''}"
    except Exception:
        return "?"


def _read_text(path, limit: int | None = None) -> str:
    try:
        data = Path(path).read_text(encoding="utf-8", errors="replace")
        return data if limit is None else data[:limit]
    except Exception:
        return ""


def _read_json(path):
    try:
        return json.loads(_read_text(path).lstrip("\ufeff"))
    except Exception:
        return None


# ==================== 网络 / 代理工具（v2.5.0） ====================
def _route_gateway() -> str:
    """默认网关 IP。

    容器内要探测宿主机上监听的端口，只能用网关地址，**不能用 host.docker.internal**：
    在 mihomo TUN + fake-ip 环境下它会解析到 198.18.x.x，任何端口探测都返回「通」，
    是彻底的假阳性（实测踩过）。
    """
    for line in _read_text("/proc/net/route").splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 3 and fields[1] == "00000000":
            try:
                return ".".join(str(int(fields[2][i:i + 2], 16)) for i in (6, 4, 2, 0))
            except Exception:
                return ""
    return ""


def _dns_servers() -> list:
    out = []
    for line in _read_text("/etc/resolv.conf").splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[0] == "nameserver":
            out.append(parts[1])
    return out[:4]


def _is_fake_ip(addr: str) -> bool:
    """198.18.0.0/15 是 mihomo TUN 的 fake-ip 段：解析结果落在这里 = DNS 已被代理接管。"""
    try:
        head = [int(x) for x in str(addr).split(".")[:2]]
        return head[0] == 198 and head[1] in (18, 19)
    except Exception:
        return False


def _mask_proxy(text: str) -> str:
    """代理地址脱敏：只留 scheme://[<带认证>]host:port，绝不带用户名 / 密码。"""
    raw = str(text or "").strip()
    if not raw:
        return ""
    try:
        parts = urllib.parse.urlsplit(raw if "://" in raw else "http://" + raw)
        if not parts.hostname:
            return "<无法解析>"
        scheme = parts.scheme or "http"
        auth = "<带认证>" if parts.username else ""
        port = f":{parts.port}" if parts.port else ""
        return f"{scheme}://{auth}{parts.hostname}{port}"
    except Exception:
        return "<无法解析>"


def _parse_proxy(text: str):
    """'socks5h://127.0.0.1:7890' → ('socks5h', '127.0.0.1', 7890)；解析失败返回 None。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        parts = urllib.parse.urlsplit(raw if "://" in raw else "http://" + raw)
        if not parts.hostname:
            return None
        scheme = (parts.scheme or "http").lower()
        default_port = 1080 if scheme.startswith("socks") else 80
        return scheme, parts.hostname, int(parts.port or default_port)
    except Exception:
        return None


def _tcp_open(host: str, port: int, timeout: float = 1.5) -> bool:
    if not host:
        return False
    sock = socket.socket()
    sock.settimeout(timeout)
    try:
        return sock.connect_ex((host, int(port))) == 0
    except Exception:
        return False
    finally:
        try:
            sock.close()
        except Exception:
            pass


def _http_probe_code(url: str, timeout: float = 2.5):
    """匿名取一次直连 HTTP 状态码（不带任何密钥）；连 401/404 也算「服务在」。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    opener.addheaders = [("User-Agent", "path_doctor")]
    try:
        with opener.open(url, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception:
        return None


def _proxy_kernels() -> list:
    """容器内扫 /proc/*/comm 找代理内核进程（不依赖 ps，容器里通常没装 procps）。"""
    found = set()
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                comm = Path(f"/proc/{pid}/comm").read_text(
                    encoding="utf-8", errors="replace").strip().lower()
            except Exception:
                continue
            for key in _PROXY_KERNELS:
                if comm == key or comm.startswith(key):
                    found.add(comm)
                    break
    except Exception:
        pass
    return sorted(found)


def _recv_exact(sock, count: int) -> bytes:
    """精确读 count 字节（TCP 不保证按写入边界返回，SOCKS5 握手必须这样读）。"""
    buf = b""
    while len(buf) < count:
        chunk = sock.recv(count - len(buf))
        if not chunk:
            raise RuntimeError("代理连接提前关闭")
        buf += chunk
    return buf


def _socks5_tunnel(proxy_host: str, proxy_port: int, dst_host: str, dst_port: int,
                   timeout: float = 6.0):
    """纯标准库 SOCKS5 CONNECT（无认证）。返回已连通的 socket；失败抛异常。

    标准库 urllib 不支持 socks，所以这里手写握手——这样「代理能不能出网」才是真的实测过。
    """
    sock = socket.create_connection((proxy_host, int(proxy_port)), timeout=timeout)
    sock.settimeout(timeout)
    try:
        sock.sendall(b"\x05\x01\x00")
        if _recv_exact(sock, 2) != b"\x05\x00":
            raise RuntimeError("SOCKS5 握手被拒（可能要求认证）")
        host = dst_host.encode("idna")
        sock.sendall(b"\x05\x01\x00\x03" + bytes([len(host)]) + host
                     + int(dst_port).to_bytes(2, "big"))
        head = _recv_exact(sock, 4)
        if head[1] != 0:
            raise RuntimeError(f"SOCKS5 连接被拒（rep={head[1]}）")
        atyp = head[3]
        if atyp == 1:
            _recv_exact(sock, 4)
        elif atyp == 3:
            _recv_exact(sock, _recv_exact(sock, 1)[0])
        elif atyp == 4:
            _recv_exact(sock, 16)
        _recv_exact(sock, 2)
        return sock
    except Exception:
        try:
            sock.close()
        except Exception:
            pass
        raise


def _proxy_https_get(scheme: str, host: str, port: int, url: str,
                     timeout: float = 6.0):
    """经代理真实取一次 HTTPS，返回 (状态码, 毫秒)。支持 http(s) 与 socks5(h)。"""
    started = time.perf_counter()
    if str(scheme).lower().startswith("socks"):
        parts = urllib.parse.urlsplit(url)
        dst_port = parts.port or (443 if parts.scheme == "https" else 80)
        raw = _socks5_tunnel(host, port, parts.hostname, dst_port, timeout=timeout)
        ctx = ssl.create_default_context()
        conn = ctx.wrap_socket(raw, server_hostname=parts.hostname)
        try:
            conn.settimeout(timeout)
            conn.sendall(
                f"GET {parts.path or '/'} HTTP/1.0\r\nHost: {parts.hostname}\r\n"
                f"User-Agent: path_doctor\r\nConnection: close\r\n\r\n".encode()
            )
            data = b""
            while b"\r\n" not in data and len(data) < 512:
                chunk = conn.recv(256)
                if not chunk:
                    break
                data += chunk
        finally:
            try:
                conn.close()
            except Exception:
                pass
        first = data.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        words = first.split()
        return (int(words[1]) if len(words) > 1 else 0), (time.perf_counter() - started) * 1000
    proxy_url = f"{'https' if str(scheme).lower() == 'https' else 'http'}://{host}:{port}"
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
    opener.addheaders = [("User-Agent", "path_doctor")]
    with opener.open(url, timeout=timeout) as resp:
        code = resp.status
    return code, (time.perf_counter() - started) * 1000


def _pad_text(text: str, width: int) -> str:
    """按显示宽度右侧补空格（CJK 记 2 列），让清单各列对齐。"""
    used = 0
    for ch in str(text):
        used += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return str(text) + " " * max(1, width - used)


def _human(num) -> str:
    try:
        num = float(num)
    except Exception:
        return str(num)
    for unit in ("B", "K", "M", "G", "T"):
        if abs(num) < 1024 or unit == "T":
            if unit in ("B", "K"):
                return f"{num:.0f}{unit}"
            return f"{num:.1f}{unit}"
        num /= 1024.0
    return f"{num:.1f}T"


def _dur(seconds) -> str:
    try:
        seconds = int(float(seconds))
    except Exception:
        return "?"
    if seconds < 0:
        return "?"
    d, rest = divmod(seconds, 86400)
    h, rest = divmod(rest, 3600)
    m, s = divmod(rest, 60)
    if d:
        return f"{d}d{h}h"
    if h:
        return f"{h}h{m}m"
    if m:
        return f"{m}m{s}s"
    return f"{s}s"


def _mode(path) -> str:
    try:
        st = os.stat(path)
        return f"{_stat.filemode(st.st_mode)} {oct(_stat.S_IMODE(st.st_mode))}"
    except Exception:
        return "?"


def _ts(epoch) -> str:
    try:
        value = float(epoch)
    except Exception:
        return ""
    if value <= 0:
        return ""
    try:
        return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def _clean(text):
    if not isinstance(text, str):
        return text
    return "".join(ch for ch in text if ord(ch) >= 32 or ch in "\t\n")


# ============================ 路径 / procfs ============================
def _apath(name: str, default: str = "") -> str:
    try:
        import astrbot.core.utils.astrbot_path as ap

        fn = getattr(ap, name, None)
        return str(fn()) if fn else default
    except Exception:
        return default


def _paths() -> dict:
    return {
        "root": _apath("get_astrbot_path", "/AstrBot"),
        "data": _apath("get_astrbot_data_path", "/AstrBot/data"),
        "temp": _apath("get_astrbot_temp_path", "/AstrBot/data/temp"),
        "plugins": _apath("get_astrbot_plugin_path", "/AstrBot/data/plugins"),
        "plugin_data": _apath("get_astrbot_plugin_data_path", "/AstrBot/data/plugin_data"),
        "config": _apath("get_astrbot_config_path", "/AstrBot/data/config"),
    }


def _loadavg():
    try:
        return tuple(float(x) for x in _read_text("/proc/loadavg").split()[:3])
    except Exception:
        return None


def _meminfo() -> dict:
    out = {}
    try:
        for line in _read_text("/proc/meminfo").splitlines():
            key, _, rest = line.partition(":")
            parts = rest.strip().split()
            if parts and parts[0].isdigit():
                out[key.strip()] = int(parts[0]) * 1024
    except Exception:
        pass
    return out


def _host_uptime():
    try:
        return float(_read_text("/proc/uptime").split()[0])
    except Exception:
        return None


def _btime():
    for line in _read_text("/proc/stat").splitlines():
        if line.startswith("btime"):
            try:
                return int(line.split()[1])
            except Exception:
                return None
    return None


def _proc_start(pid: int = 1):
    try:
        raw = _read_text(f"/proc/{pid}/stat")
        fields = raw[raw.rindex(")") + 2:].split()
        start_ticks = int(fields[19])
        base = _btime()
        hz = os.sysconf("SC_CLK_TCK")
        return (base + start_ticks / hz) if base else None
    except Exception:
        return None


def _mount_of(target: str):
    """返回 {mnt, root, fstype, src}，取最长匹配。

    mountinfo 里第 4 段是本挂载在宿主文件系统里的根路径——bind mount 时
    它就是宿主上的真实目录（例如 /home/<user>/bot-stack/astrbot/data）。
    """
    try:
        target = str(Path(target).resolve())
    except Exception:
        target = str(target)
    best = None
    for line in _read_text("/proc/self/mountinfo").splitlines():
        parts = line.split()
        if "-" not in parts:
            continue
        sep = parts.index("-")
        mnt = parts[4].replace("\\040", " ")
        fstype = parts[sep + 1]
        src = parts[sep + 2].replace("\\040", " ") if len(parts) > sep + 2 else ""
        root = parts[3].replace("\\040", " ") if len(parts) > 3 else ""
        if target == mnt or target.startswith(mnt.rstrip("/") + "/"):
            if best is None or len(mnt) > len(best["mnt"]):
                best = {"mnt": mnt, "root": root, "fstype": fstype, "src": src}
    return best


def _mount_text(mount) -> str:
    if not mount:
        return "未识别（可能不是 bind mount）"
    if mount["root"] and mount["root"] != "/":
        return f"宿主路径 {mount['root']}（bind，{mount['fstype']}）"
    return f"设备 {mount['src']}（{mount['fstype']}）"


def _tcp_ports() -> dict:
    """{port: {"listen": bool, "estab": int}}，来自 /proc/net/tcp{,6}。"""
    out = {}
    for fn in ("/proc/net/tcp", "/proc/net/tcp6"):
        for line in _read_text(fn).splitlines()[1:]:
            parts = line.split()
            if len(parts) < 4:
                continue
            try:
                port = int(parts[1].split(":")[-1], 16)
            except Exception:
                continue
            slot = out.setdefault(port, {"listen": False, "estab": 0})
            state = parts[3]
            if state == "0A":
                slot["listen"] = True
            elif state == "01":
                slot["estab"] += 1
    return out


def _dir_stats(path) -> tuple:
    """(文件数, 字节数)"""
    files = 0
    total = 0
    try:
        for root, _dirs, names in os.walk(path, onerror=lambda e: None):
            for name in names:
                files += 1
                try:
                    total += os.stat(os.path.join(root, name)).st_size
                except OSError:
                    pass
            if files > 20000:
                break
    except Exception:
        pass
    return files, total


def _statvfs(path):
    try:
        st = os.statvfs(path)
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used = total - st.f_bfree * st.f_frsize
        inodes = st.f_files
        ifree = st.f_favail
        return {
            "total": total,
            "free": free,
            "used": used,
            "pct": (used / total * 100) if total else 0,
            "inodes": inodes,
            "ifree": ifree,
            "ipct": ((inodes - ifree) / inodes * 100) if inodes else 0,
        }
    except Exception:
        return None


def _parse_meta(path) -> dict:
    text = _read_text(path)
    if not text:
        return {}
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(text) or {}
        if isinstance(data, dict):
            return {str(k): v for k, v in data.items()}
    except Exception:
        pass
    data = {}
    for line in text.splitlines():
        if ":" in line and not line.strip().startswith("#"):
            key, _, value = line.partition(":")
            value = value.strip().strip('"').strip("'")
            if value:
                data[key.strip()] = value
    return data


_REQ_ALIAS = {
    "pyyaml": "yaml",
    "pillow": "PIL",
    "beautifulsoup4": "bs4",
    "python-dateutil": "dateutil",
    "pycryptodome": "Crypto",
    "opencv-python": "cv2",
}


def _module_of(requirement: str) -> str:
    name = re.split(r"[<>=!~;\[\]\s]", requirement.strip(), maxsplit=1)[0].strip()
    return _REQ_ALIAS.get(name.lower(), name)


def _module_exists(module: str) -> bool:
    if not module:
        return False
    try:
        import importlib.util

        return importlib.util.find_spec(module) is not None
    except Exception:
        return False


def _dist_version(name: str):
    """按 pip 分发包名查已安装版本（fpdf2/opencc-python-reimplemented 这类包名
    与 import 名不一致，只能查元数据）。"""
    if not name:
        return None
    try:
        import importlib.metadata as metadata

        return metadata.version(name)
    except Exception:
        return None


def _requirements(path) -> list:
    """读取 requirements.txt：既支持一行一个（PEP 508），也支持 JSON 字典形式
    （部分插件写成 {"pyyaml": ">=6.0"}）。返回 [(包名, 约束)]。"""
    text = _read_text(path)
    if not text.strip():
        return []
    stripped = text.strip()
    if stripped.startswith("{"):
        data = _read_json(path)
        if isinstance(data, dict):
            return [(str(k), str(v or "")) for k, v in data.items()]
    out = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        parts = re.split(r"(?=[<>=!~])", line, maxsplit=1)
        name = parts[0].strip()
        spec = parts[1].strip() if len(parts) > 1 else ""
        if name:
            out.append((name, spec))
    return out


def _req_status(name: str, spec: str) -> str:
    """返回 ok / missing / mismatch:<原因>"""
    version = _dist_version(name)
    if version is None:
        module = _REQ_ALIAS.get(name.lower(), name).replace("-", "_")
        if _module_exists(module) or _module_exists(name.replace("-", "_")):
            return "ok"
        return "missing"
    if spec:
        try:
            from packaging.specifiers import SpecifierSet
            from packaging.version import Version

            if Version(str(version)) not in SpecifierSet(spec):
                return f"mismatch:装的是 {version}，要求 {spec}"
        except Exception:
            pass
    return "ok"


def _call_args(code: str, func: str = "register") -> list[str]:
    """提取 func(...) 的完整实参文本（括号配对）。"""
    out = []
    for match in re.finditer(re.escape(func) + r"\(", code):
        start = match.end()
        depth = 1
        idx = start
        while idx < len(code) and depth > 0:
            ch = code[idx]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            idx += 1
        if depth == 0:
            out.append(code[start:idx - 1])
    return out


# ============================ Excel / CSV ============================
# 列定义：(显示名, 宽度, 类型)。类型决定对齐与数字格式。
# 单群页不重复「群号/群名」（标题里已有），多群平铺页才带上。
_EXPORT_COLUMNS = [
    ("序号", 7, "num"),
    ("群号", 13, "text"),
    ("群名", 22, "str"),
    ("QQ号", 14, "text"),
    ("昵称", 20, "str"),
    ("群名片", 16, "str"),
    ("角色", 9, "str"),
    ("等级", 8, "num"),
    ("头衔", 14, "str"),
    ("加入时间", 17, "date"),
    ("最后发言", 17, "date"),
    ("沉默天数", 12, "num"),
    ("禁言到期", 17, "date"),
    ("不良记录", 11, "str"),
]
_DETAIL_COLUMNS = [c for c in _EXPORT_COLUMNS if c[0] not in ("群号", "群名")]
_EXCEL_HEADERS = [c[0] for c in _EXPORT_COLUMNS]

_ROLE_TEXT = {"owner": "群主", "admin": "管理员", "member": "成员"}

_C_HEADER_BG = "1F4E79"
_C_HEADER_FG = "FFFFFF"
_C_ZEBRA = "F4F8FC"
_C_BORDER = "C9D6E3"
_C_TITLE = "12324F"
_C_MUTED = "7F7F7F"


def _sanitize_sheet(name: str, used: set) -> str:
    base = re.sub(r"[\[\]:*?/\\]", "_", str(name))[:28] or "Sheet"
    candidate = base
    idx = 2
    while candidate.lower() in used:
        suffix = f"_{idx}"
        candidate = base[: 28 - len(suffix)] + suffix
        idx += 1
    used.add(candidate.lower())
    return candidate


def _excel_available() -> bool:
    try:
        import importlib.util

        return importlib.util.find_spec("openpyxl") is not None
    except Exception:
        return False


def _dt(value):
    """时间戳 → datetime（0/空/非法返回 None）。"""
    try:
        v = float(value)
    except Exception:
        return None
    if v <= 0:
        return None
    try:
        return datetime.fromtimestamp(v)
    except Exception:
        return None


def _iso(value):
    """datetime → 'YYYY-mm-dd HH:MM:SS'（None 保持 None，便于 JSON 消费）。"""
    return value.strftime("%Y-%m-%d %H:%M:%S") if value else None


def _member_record(member, group_id, group_name: str) -> dict:
    """把协议端返回的成员对象规整成导出用的一条记录。"""
    m = member if isinstance(member, dict) else {}
    last = m.get("last_sent_time")
    silent = None
    try:
        last_f = float(last)
        if last_f > 0:
            silent = max(0, int((time.time() - last_f) // 86400))
    except Exception:
        silent = None
    role = str(m.get("role", "") or "")
    level = m.get("level", "")
    try:
        level = int(level)
    except Exception:
        level = None
    return {
        "gid": str(group_id),
        "gname": _clean(group_name or ""),
        "uid": str(m.get("user_id", "") or ""),
        "nick": _clean(m.get("nickname", "") or ""),
        "card": _clean(m.get("card", "") or ""),
        "role": role,
        "level": level,
        "title": _clean(m.get("title", "") or ""),
        "join": _dt(m.get("join_time")),
        "last": _dt(last),
        "silent": silent,
        "shut": _dt(m.get("shut_up_timestamp")),
        "unfriendly": bool(m.get("unfriendly")),
    }


def _record_dict(idx: int, rec: dict) -> dict:
    return {
        "序号": idx,
        "群号": rec["gid"],
        "群名": rec["gname"],
        "QQ号": rec["uid"],
        "昵称": rec["nick"],
        "群名片": rec["card"] or "—",
        "角色": _ROLE_TEXT.get(rec["role"], rec["role"] or "—"),
        "等级": rec["level"],
        "头衔": rec["title"] or "—",
        "加入时间": rec["join"],
        "最后发言": rec["last"],
        "沉默天数": rec["silent"],
        "禁言到期": rec["shut"],
        "不良记录": "是" if rec["unfriendly"] else "否",
    }


def _rows_for(idx_base: int, records: list, columns: list) -> list:
    rows = []
    for offset, rec in enumerate(records):
        data = _record_dict(idx_base + offset, rec)
        rows.append([data[name] for name, _w, _t in columns])
    return rows


def _record_row(idx: int, rec: dict) -> list:
    """兼容旧调用：按完整 14 列输出。"""
    data = _record_dict(idx, rec)
    return [data[name] for name, _w, _t in _EXPORT_COLUMNS]


def _block_stats(records: list) -> dict:
    roles = {"owner": 0, "admin": 0, "member": 0}
    for rec in records:
        key = rec["role"] if rec["role"] in roles else "member"
        roles[key] += 1
    silent30 = sum(1 for r in records if r["silent"] is not None and r["silent"] > 30)
    silent90 = sum(1 for r in records if r["silent"] is not None and r["silent"] > 90)
    never = sum(1 for r in records if r["silent"] is None)
    active7 = sum(1 for r in records if r["silent"] is not None and r["silent"] <= 7)
    return {
        "total": len(records),
        "owner": roles["owner"],
        "admin": roles["admin"],
        "member": roles["member"],
        "active7": active7,
        "silent30": silent30,
        "silent90": silent90,
        "never": never,
    }


def _write_csv(rows: list) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    for row in rows:
        writer.writerow(row)
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


def _write_member_workbook(blocks: list, meta: dict) -> bytes:
    """生成带样式的成员名单工作簿。

    blocks: [{"gid","gname","records":[...],"note":""}]
    结构：汇总页（统计 + 图表）+ 每个群一页；群数过多时退化为「汇总 + 全部成员」两页。
    """
    from openpyxl import Workbook
    from openpyxl.formatting.rule import CellIsRule, DataBarRule
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.properties import PageSetupProperties

    thin = Side(style="thin", color=_C_BORDER)
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    header_fill = PatternFill("solid", fgColor=_C_HEADER_BG)
    zebra_fill = PatternFill("solid", fgColor=_C_ZEBRA)
    base_font = Font(name="微软雅黑", size=10)
    header_font = Font(name="微软雅黑", size=10, bold=True, color=_C_HEADER_FG)
    title_font = Font(name="微软雅黑", size=14, bold=True, color=_C_TITLE)
    muted_font = Font(name="微软雅黑", size=9, color=_C_MUTED)

    ncols = len(_EXPORT_COLUMNS)
    wb = Workbook()
    wb.remove(wb.active)
    wb.properties.title = f"群成员导出 · {len(blocks)} 群"
    wb.properties.creator = "path_doctor"
    wb.properties.description = (
        f"path_doctor {meta.get('version', '')} · 生成于 {meta.get('generated', '')}"
    )

    # ---------- 汇总页 ----------
    summary = wb.create_sheet("汇总")
    summary.sheet_properties.tabColor = "548235"
    summary.sheet_view.showGridLines = False
    summary.merge_cells(start_row=1, start_column=1, end_row=1, end_column=10)
    cell = summary.cell(row=1, column=1, value="群成员数据汇总")
    cell.font = title_font
    cell.alignment = Alignment(vertical="center")
    summary.row_dimensions[1].height = 30

    info = [
        ("生成时间", meta.get("generated", "")),
        ("数据来源", meta.get("source", "")),
        ("机器人", meta.get("bot", "")),
        ("导出工具", f"path_doctor {meta.get('version', '')}"),
    ]
    for i, (key, value) in enumerate(info, start=3):
        summary.cell(row=i, column=1, value=key).font = Font(name="微软雅黑", size=9, bold=True,
                                                             color=_C_MUTED)
        summary.cell(row=i, column=2, value=value).font = muted_font
        summary.merge_cells(start_row=i, start_column=2, end_row=i, end_column=5)

    headers = ["群号", "群名", "成员数", "群主", "管理员", "普通成员",
               "近 7 天发言", "30 天未发言", "90 天未发言", "状态"]
    header_row = 8
    for col, name in enumerate(headers, start=1):
        c = summary.cell(row=header_row, column=col, value=name)
        c.fill = header_fill
        c.font = header_font
        c.alignment = Alignment(horizontal="center", vertical="center")
        c.border = border
    summary.row_dimensions[header_row].height = 24

    total = {"total": 0, "owner": 0, "admin": 0, "member": 0,
             "active7": 0, "silent30": 0, "silent90": 0}
    row = header_row + 1
    for block in blocks:
        records = block.get("records") or []
        st = _block_stats(records)
        for key in total:
            total[key] += st[key]
        values = [
            block["gid"], block["gname"] or "—", st["total"], st["owner"], st["admin"],
            st["member"], st["active7"], st["silent30"], st["silent90"],
            block.get("note") or ("空群" if not records else "OK"),
        ]
        for col, value in enumerate(values, start=1):
            c = summary.cell(row=row, column=col, value=value)
            c.font = base_font
            c.border = border
            c.alignment = Alignment(
                horizontal="center" if col not in (2,) else "left", vertical="center"
            )
        summary.row_dimensions[row].height = 20
        row += 1

    total_row = row
    values = ["合计", f"{len(blocks)} 个群", total["total"], total["owner"], total["admin"],
              total["member"], total["active7"], total["silent30"], total["silent90"], ""]
    for col, value in enumerate(values, start=1):
        c = summary.cell(row=total_row, column=col, value=value)
        c.font = Font(name="微软雅黑", size=10, bold=True)
        c.fill = PatternFill("solid", fgColor="E2EFDA")
        c.border = border
        c.alignment = Alignment(horizontal="center" if col != 2 else "left", vertical="center")

    for col, width in enumerate([13, 26, 9, 7, 8, 10, 12, 13, 13, 12], start=1):
        summary.column_dimensions[get_column_letter(col)].width = width

    notes = [str(b.get("note")) for b in blocks if b.get("note")]
    if notes:
        note_row = total_row + 2
        summary.merge_cells(start_row=note_row, start_column=1,
                            end_row=note_row, end_column=10)
        c = summary.cell(row=note_row, column=1, value="备注：" + "；".join(notes[:5]))
        c.font = Font(name="微软雅黑", size=9, color="9C0006")
        c.alignment = Alignment(vertical="center", wrap_text=True)

    # 图表：多群看人数分布，单群看角色构成
    try:
        from openpyxl.chart import BarChart, PieChart, Reference

        if len(blocks) > 1:
            chart = BarChart()
            chart.type = "col"
            chart.style = 10
            chart.title = "各群成员数"
            chart.y_axis.title = "人数"
            data = Reference(summary, min_col=3, min_row=header_row,
                             max_row=header_row + len(blocks))
            cats = Reference(summary, min_col=2, min_row=header_row + 1,
                             max_row=header_row + len(blocks))
            chart.add_data(data, titles_from_data=True)
            chart.set_categories(cats)
            chart.height = 8
            chart.width = 15
            summary.add_chart(chart, "L8")
        else:
            st = _block_stats(blocks[0].get("records") or [])
            role_row = header_row + 4
            summary.cell(row=role_row, column=1, value="角色").font = muted_font
            summary.cell(row=role_row, column=2, value="人数").font = muted_font
            for i, (label, key) in enumerate(
                (("群主", "owner"), ("管理员", "admin"), ("普通成员", "member")), start=1
            ):
                summary.cell(row=role_row + i, column=1, value=label).font = base_font
                summary.cell(row=role_row + i, column=2, value=st[key]).font = base_font
            pie = PieChart()
            pie.title = "角色构成"
            pie.height = 7
            pie.width = 10
            data = Reference(summary, min_col=2, min_row=role_row,
                             max_row=role_row + 3)
            labels = Reference(summary, min_col=1, min_row=role_row + 1,
                               max_row=role_row + 3)
            pie.add_data(data, titles_from_data=True)
            pie.set_categories(labels)
            summary.add_chart(pie, "L8")
    except Exception:
        pass

    # ---------- 每个群一页 ----------
    detail_blocks = blocks if len(blocks) <= 30 else [
        {
            "gid": "全部",
            "gname": "全部群",
            "records": [r for b in blocks for r in (b.get("records") or [])],
            "note": "",
        }
    ]
    used: set = {"汇总"}
    for block in detail_blocks:
        gid = str(block["gid"])
        gname = _clean(block.get("gname") or "")
        title = f"{gname}（{gid}）" if gname and gid != "全部" else (gname or gid)
        # 单群页不重复群号/群名；群数过多退化成的「全部」页才带
        columns = _EXPORT_COLUMNS if gid == "全部" else _DETAIL_COLUMNS
        ncols = len(columns)
        col_index = {name: i + 1 for i, (name, _w, _t) in enumerate(columns)}

        ws = wb.create_sheet(_sanitize_sheet(f"{title}"[:28] or f"G{gid}", used))
        ws.sheet_properties.tabColor = "2E75B6"
        ws.sheet_view.showGridLines = False

        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncols)
        cell = ws.cell(row=1, column=1, value=f"群成员名单 · {title}")
        cell.font = title_font
        cell.alignment = Alignment(vertical="center")
        ws.row_dimensions[1].height = 28

        records = block.get("records") or []
        st = _block_stats(records)
        subtitle = (
            f"共 {st['total']} 人 ｜ 群主 {st['owner']} · 管理员 {st['admin']} · 成员 {st['member']} ｜ "
            f"近 7 天发言 {st['active7']} ｜ 30 天未发言 {st['silent30']} ｜ 90 天未发言 {st['silent90']}"
        )
        ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=ncols)
        cell = ws.cell(row=2, column=1, value=subtitle)
        cell.font = muted_font
        cell.alignment = Alignment(vertical="center")
        ws.row_dimensions[2].height = 20

        header_row = 3
        for col, (name, width, _kind) in enumerate(columns, start=1):
            c = ws.cell(row=header_row, column=col, value=name)
            c.fill = header_fill
            c.font = header_font
            c.border = border
            c.alignment = Alignment(horizontal="center", vertical="center")
            ws.column_dimensions[get_column_letter(col)].width = width
        ws.row_dimensions[header_row].height = 24

        ordered = sorted(
            records,
            key=lambda r: (
                {"owner": 0, "admin": 1}.get(r["role"], 2),
                r["join"] or datetime(1970, 1, 1),
                r["uid"],
            ),
        )
        for idx, values in enumerate(_rows_for(1, ordered, columns), start=1):
            row = header_row + idx
            for col, ((name, _w, kind), value) in enumerate(zip(columns, values), start=1):
                c = ws.cell(row=row, column=col, value=value)
                c.font = base_font
                c.border = border
                c.alignment = Alignment(
                    horizontal="left" if kind == "str" else "center",
                    vertical="center",
                )
                if idx % 2 == 0:
                    c.fill = zebra_fill
                if kind == "text":
                    c.number_format = "@"  # 群号 / QQ 号按文本存，避免科学计数法
                elif kind == "date":
                    c.number_format = "yyyy-mm-dd hh:mm"
                    if value is None:
                        c.value = "—"
            ws.row_dimensions[row].height = 19
            # 角色着色
            role_cell = ws.cell(row=row, column=col_index.get("角色", 1))
            role_fill = {
                "群主": ("FFE699", "7F6000"),
                "管理员": ("DDEBF7", "1F4E79"),
            }.get(role_cell.value)
            if role_fill:
                role_cell.fill = PatternFill("solid", fgColor=role_fill[0])
                role_cell.font = Font(name="微软雅黑", size=10, bold=True, color=role_fill[1])

        last_row = header_row + len(ordered)
        if ordered:
            ws.auto_filter.ref = f"A{header_row}:{get_column_letter(ncols)}{last_row}"
            # 沉默天数：数据条 + 超 90 天红字
            silent_col = col_index.get("沉默天数")
            try:
                if silent_col:
                    letter = get_column_letter(silent_col)
                    span = f"{letter}{header_row + 1}:{letter}{last_row}"
                    ws.conditional_formatting.add(
                        span,
                        DataBarRule(start_type="num", start_value=0, end_type="num",
                                    end_value=max(365, max((r["silent"] or 0) for r in ordered)),
                                    color="9DC3E6", showValue=True),
                    )
                    ws.conditional_formatting.add(
                        span,
                        CellIsRule(operator="greaterThan", formula=["90"],
                                   font=Font(name="微软雅黑", size=10, color="9C0006"),
                                   fill=PatternFill("solid", fgColor="FFC7CE")),
                    )
            except Exception:
                pass

        ws.freeze_panes = f"D{header_row + 1}"
        try:
            ws.page_setup.orientation = "landscape"
            ws.page_setup.fitToWidth = 1
            ws.page_setup.fitToHeight = 0
            ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
            ws.print_title_rows = f"{header_row}:{header_row}"
        except Exception:
            pass

        if block.get("note"):
            note_row = last_row + 2
            ws.merge_cells(start_row=note_row, start_column=1,
                           end_row=note_row, end_column=ncols)
            c = ws.cell(row=note_row, column=1, value=f"注：{block['note']}")
            c.font = Font(name="微软雅黑", size=9, color="9C0006")

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _member_row(member: dict, group_id, group_name: str) -> list:
    """兼容旧调用：单条记录 → 行。"""
    return _record_row(1, _member_record(member, group_id, group_name))


# ============================ 体检工作簿 ============================
def _diag_styles():
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    thin = Side(style="thin", color=_C_BORDER)
    return {
        "border": Border(left=thin, right=thin, top=thin, bottom=thin),
        "header_fill": PatternFill("solid", fgColor=_C_HEADER_BG),
        "header_font": Font(name="微软雅黑", size=10, bold=True, color=_C_HEADER_FG),
        "zebra": PatternFill("solid", fgColor=_C_ZEBRA),
        "title_font": Font(name="微软雅黑", size=15, bold=True, color=_C_TITLE),
        "sub_font": Font(name="微软雅黑", size=9, color=_C_MUTED),
        "section_font": Font(name="微软雅黑", size=11, bold=True, color=_C_TITLE),
        "body_font": Font(name="微软雅黑", size=10),
        "bold_font": Font(name="微软雅黑", size=10, bold=True),
        "ok_fill": PatternFill("solid", fgColor="E7F6EF"),
        "warn_fill": PatternFill("solid", fgColor="FDF3E2"),
        "bad_fill": PatternFill("solid", fgColor="FDECEB"),
        "ok_font": Font(name="微软雅黑", size=10, bold=True, color="0F8A5F"),
        "warn_font": Font(name="微软雅黑", size=10, bold=True, color="A86414"),
        "bad_font": Font(name="微软雅黑", size=10, bold=True, color="B0342C"),
        "center": Alignment(horizontal="center", vertical="center"),
        "left": Alignment(horizontal="left", vertical="center"),
        "wrap": Alignment(horizontal="left", vertical="center", wrap_text=True),
    }


def _ws_head(ws, title: str, subtitle: str, ncols: int, st: dict) -> int:
    """标题 + 副标题，返回下一个可用行号。"""
    from openpyxl.styles import Alignment

    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncols)
    cell = ws.cell(row=1, column=1, value=title)
    cell.font = st["title_font"]
    cell.alignment = Alignment(vertical="center")
    ws.row_dimensions[1].height = 30
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=ncols)
    cell = ws.cell(row=2, column=1, value=subtitle)
    cell.font = st["sub_font"]
    cell.alignment = Alignment(vertical="center")
    ws.row_dimensions[2].height = 18
    ws.sheet_view.showGridLines = False
    return 4


def _ws_section(ws, row: int, text: str, ncols: int, st: dict) -> int:
    from openpyxl.styles import Alignment

    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=ncols)
    cell = ws.cell(row=row, column=1, value=text)
    cell.font = st["section_font"]
    cell.alignment = Alignment(vertical="center")
    ws.row_dimensions[row].height = 22
    return row + 1


def _ws_table(ws, row: int, headers: list, rows: list, widths: list, st: dict,
              aligns: list = None, fills: list = None, wrap_cols: list = None,
              row_height: int = 19) -> tuple:
    """写一张表：表头 + 数据，返回 (下一行, 表头行)。"""
    from openpyxl.styles import Alignment
    from openpyxl.utils import get_column_letter

    for idx, (name, width) in enumerate(zip(headers, widths), start=1):
        c = ws.cell(row=row, column=idx, value=name)
        c.fill = st["header_fill"]
        c.font = st["header_font"]
        c.border = st["border"]
        c.alignment = st["center"]
        ws.column_dimensions[get_column_letter(idx)].width = width
    ws.row_dimensions[row].height = 22
    header_row = row
    row += 1
    for ridx, values in enumerate(rows, start=1):
        lines_needed = 1
        for cidx, value in enumerate(values, start=1):
            c = ws.cell(row=row, column=cidx, value=value)
            c.font = st["body_font"]
            c.border = st["border"]
            align = (aligns[cidx - 1] if aligns and cidx - 1 < len(aligns) else "left")
            if wrap_cols and cidx in wrap_cols:
                c.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
                if isinstance(value, str):
                    per_line = max(8, int(widths[cidx - 1] * 1.05))
                    # CJK 按 2 列折算：否则中文长文本会被低估行数，下半截被行高裁掉
                    wide = sum(1 for ch in value
                               if unicodedata.east_asian_width(ch) in ("W", "F"))
                    effective = len(value) + wide
                    lines_needed = max(lines_needed, (effective + per_line - 1) // per_line)
            else:
                c.alignment = st["center"] if align == "center" else st["left"]
            if ridx % 2 == 0:
                c.fill = st["zebra"]
        if fills:
            fills(ws, row, values)
        ws.row_dimensions[row].height = max(row_height, min(76, lines_needed * 15 + 4))
        row += 1
    return row, header_row



def _write_diag_workbook(snap: dict, shared: dict, write: dict, caps: dict) -> bytes:
    """把一次体检渲染成多页工作簿：一页一张表，形状统一，宽度不打架。"""
    from openpyxl import Workbook
    from openpyxl.worksheet.properties import PageSetupProperties

    st = _diag_styles()
    wb = Workbook()
    wb.remove(wb.active)
    wb.properties.title = f"path_doctor 体检报告 {snap['tool']['version']}"
    wb.properties.creator = "path_doctor"
    wb.properties.description = f"生成于 {snap['generated']}"

    host = snap.get("host") or {}
    core = snap.get("astrbot") or {}
    plug = snap.get("plugins") or {}
    cont = snap.get("container") or {}
    proto = snap.get("protocol") or {}
    net = snap.get("net") or {}
    nverdicts = net.get("verdicts") or {}
    disk = host.get("disk") or {}
    mem_total = host.get("mem_total") or 0
    mem_avail = host.get("mem_available") or 0
    mem_pct = round((mem_total - mem_avail) / mem_total * 100) if mem_total else 0
    proto_text = _fmt_caps_protocol(caps.get("protocol") or proto.get("protocol") or "")
    summary = caps.get("summary") or {}

    def _finish(sheet, widths):
        from openpyxl.utils import get_column_letter

        for idx, width in enumerate(widths, start=1):
            sheet.column_dimensions[get_column_letter(idx)].width = width
        try:
            sheet.page_setup.orientation = "landscape"
            sheet.page_setup.fitToWidth = 1
            sheet.page_setup.fitToHeight = 0
            sheet.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
        except Exception:
            pass

    # ---------------- 总览 ----------------
    ws = wb.create_sheet("总览")
    ws.sheet_properties.tabColor = "548235"
    row = _ws_head(ws, f"path_doctor 体检报告 {snap['tool']['version']}",
                   f"{snap['generated']} · {snap['tz']}", 4, st)
    overview_rows = [
        ["环境", "AstrBot", f"{core.get('version', '?')} · Python {core.get('python', '?')}", "✅"],
        ["环境", "协议端", proto_text, "✅"],
        ["环境", "容器 / 宿主", f"{cont.get('id', '?')} → {cont.get('host_path') or '-'}", "✅"],
        ["环境", "平台适配器", f"{len(snap.get('platforms') or [])} 个", "✅"],
        ["插件", "目录 / 已加载", f"{plug.get('total', 0)} / {plug.get('loaded', 0)}", "✅"],
        ["插件", "异常 / 警告", f"{plug.get('problem_count', 0)} / {plug.get('warning_count', 0)}",
         "⚠️" if (plug.get("problem_count") or plug.get("warning_count")) else "✅"],
        ["框架", "事件循环延迟",
         f"{(core.get('loop_lag_ms') if core.get('loop_lag_ms') is not None else -1):.1f} ms",
         "✅" if 0 <= (core.get("loop_lag_ms") or 0) < 100 else "⚠️"],
        ["协议端", "API 通过 / 总数", f"{summary.get('ok', 0)} / {len(caps.get('results') or [])}", "✅"],
        ["宿主机", "内存使用率", f"{mem_pct}%", "⚠️" if mem_pct > 85 else "✅"],
        ["宿主机", "磁盘使用率", f"{round(disk.get('pct', 0))}%",
         "⚠️" if (disk.get("pct") or 0) > 85 else "✅"],
        ["宿主机", "inode 使用率", f"{round(disk.get('ipct', 0))}%",
         "⚠️" if (disk.get("ipct") or 0) > 85 else "✅"],
        ["网络", "结论", net.get("verdict", "-"), "✅" if net.get("ok") else "❌"],
        ["网络", "国内 / 海外 / 代理",
         "国内 %s · 海外 %s · 代理 %s" % (nverdicts.get("domestic", "-"),
                                          nverdicts.get("overseas", "-"),
                                          nverdicts.get("proxy", "-")),
         "✅" if net.get("usable") else "❌"],
    ]

    def _overview_fill(ws_, r, values):
        cell = ws_.cell(row=r, column=4)
        mark = values[3]
        if mark == "✅":
            cell.fill, cell.font = st["ok_fill"], st["ok_font"]
        elif mark == "⚠️":
            cell.fill, cell.font = st["warn_fill"], st["warn_font"]
        else:
            cell.fill, cell.font = st["bad_fill"], st["bad_font"]

    row, header_row = _ws_table(ws, row, ["维度", "指标", "值", "状态"], overview_rows,
                                [10, 22, 62, 8], st,
                                aligns=["left", "left", "left", "center"],
                                fills=_overview_fill, wrap_cols=[3])
    row += 1
    row = _ws_section(ws, row, "问题清单", 4, st)
    issues = []
    for item in plug.get("items") or []:
        for text in (item.get("problems") or []):
            issues.append(["异常", item["dir"], text])
        for text in (item.get("warnings") or []):
            issues.append(["警告", item["dir"], text])

    def _issue_fill(ws_, r, values):
        cell = ws_.cell(row=r, column=1)
        if values[0] == "异常":
            cell.font = st["bad_font"]
            cell.fill = st["bad_fill"]
        else:
            cell.font = st["warn_font"]
            cell.fill = st["warn_fill"]

    if issues:
        row, header_row2 = _ws_table(ws, row, ["级别", "对象", "说明"], issues,
                                     [10, 34, 58], st,
                                     aligns=["center", "left", "left"],
                                     fills=_issue_fill, wrap_cols=[3])
        ws.freeze_panes = None
    else:
        ws.cell(row=row, column=1, value="✅ 没有发现问题").font = st["ok_font"]
    _finish(ws, [10, 34, 54, 8])

    # ---------------- 插件 ----------------
    ws = wb.create_sheet("插件")
    ws.sheet_properties.tabColor = "2E75B6"
    row = _ws_head(ws, "插件体检",
                   f"{plug.get('total', 0)} 个目录 · 已加载 {plug.get('loaded', 0)} · "
                   f"异常 {plug.get('problem_count', 0)} · 警告 {plug.get('warning_count', 0)}", 8, st)
    plugin_rows = []
    for item in sorted(plug.get("items") or [],
                       key=lambda i: (-(len(i.get("problems") or []) + len(i.get("warnings") or [])),
                                      i["dir"])):
        notes = "；".join((item.get("problems") or []) + (item.get("warnings") or []))
        label = item["dir"]
        meta_name = item.get("name") or ""
        if meta_name and meta_name != item["dir"]:
            label = f"{item['dir']}（{meta_name}）"
        author = item.get("author") or "-"
        if len(author) > 26:
            author = author[:26] + "…"
        plugin_rows.append([
            label,
            item.get("version") or "-",
            author,
            "已加载" if item.get("loaded") else ("已停用" if item.get("disabled") else "未加载"),
            "✅" if item.get("config") else "-",
            _human(item.get("bytes", 0)),
            item.get("files", 0),
            notes or "-",
        ])

    def _plugin_fill(ws_, r, values):
        cell = ws_.cell(row=r, column=4)
        if values[3] == "已加载":
            cell.font = st["ok_font"]
        elif values[3] == "未加载":
            cell.font, cell.fill = st["bad_font"], st["bad_fill"]
        else:
            cell.font, cell.fill = st["warn_font"], st["warn_fill"]
        if values[7] and values[7] != "-":
            ws_.cell(row=r, column=8).font = st["warn_font"]

    row, header_row = _ws_table(ws, row,
                                ["插件目录", "版本", "作者", "状态", "配置", "体积", "文件数", "问题"],
                                plugin_rows, [42, 10, 28, 8, 6, 8, 8, 56], st,
                                aligns=["left", "center", "left", "center", "center",
                                        "center", "center", "left"],
                                fills=_plugin_fill, wrap_cols=[8])
    if plugin_rows:
        ws.auto_filter.ref = f"A{header_row}:H{row - 1}"
        ws.freeze_panes = f"A{header_row + 1}"
    _finish(ws, [46, 10, 26, 8, 8, 8, 9, 54])

    # ---------------- 框架 ----------------
    ws = wb.create_sheet("框架")
    ws.sheet_properties.tabColor = "7030A0"
    row = _ws_head(ws, "AstrBot 框架",
                   f"{core.get('version', '?')} · Python {core.get('python', '?')}", 2, st)
    core_rows = [
        ["版本", core.get("version", "?")],
        ["Python", core.get("python", "?")],
        ["启动时间", core.get("started_at") or "-"],
        ["运行时长", f"{round((core.get('uptime_s') or 0) / 60)} 分钟"],
        ["事件循环延迟",
         f"{(core.get('loop_lag_ms') if core.get('loop_lag_ms') is not None else -1):.1f} ms"],
        ["配置文件版本", str(core.get("config_version", "-"))],
        ["日志级别", str(core.get("log_level", "-"))],
        ["文件日志", "开启" if core.get("log_file_enable") else "未开启（只能看 docker logs）"],
        ["WebUI 端口", f":{core.get('webui_port', '-')}"],
        ["wake_prefix", str(core.get("wake_prefix", "-"))],
        ["http_proxy", core.get("http_proxy") or "<未设置>"],
        ["管理员", ", ".join(map(str, core.get("admins") or [])) or "-"],
    ]
    for plat in snap.get("platforms") or []:
        core_rows.append([
            f"平台 {plat['id']}",
            f"{plat['type']} :{plat['port']} · "
            f"{'在线（' + str(plat['estab']) + ' 连接）' if plat['estab'] else ('监听中·无连接' if plat['listen'] else '未监听')}",
        ])
    _ws_table(ws, row, ["项", "值"], core_rows, [24, 84], st)
    _finish(ws, [24, 84])

    # ---------------- 协议端 ----------------
    ws = wb.create_sheet("协议端")
    ws.sheet_properties.tabColor = "C55A11"
    row = _ws_head(ws, "协议端实测",
                   f"{proto_text} · 群 {caps.get('group_id') or '-'} · "
                   f"用户 {caps.get('user_id') or '-'} · "
                   f"✅{summary.get('ok', 0)} ⚠️{summary.get('unsupported', 0)} "
                   f"❌{summary.get('error', 0)} ⏱{summary.get('timeout', 0)}", 4, st)
    marks = {"ok": "✅", "unsupported": "⚠️", "error": "❌", "timeout": "⏱"}
    api_rows = [[item["action"], marks.get(item["status"], "?"), round(item["ms"]),
                 item.get("detail") or item.get("error") or ""]
                for item in caps.get("results") or []]

    def _api_fill(ws_, r, values):
        cell = ws_.cell(row=r, column=2)
        if values[1] == "✅":
            cell.font, cell.fill = st["ok_font"], st["ok_fill"]
        elif values[1] == "⚠️":
            cell.font, cell.fill = st["warn_font"], st["warn_fill"]
        else:
            cell.font, cell.fill = st["bad_font"], st["bad_fill"]

    row, header_row = _ws_table(ws, row, ["API", "状态", "耗时(ms)", "结果"], api_rows,
                                [30, 8, 12, 76], st,
                                aligns=["left", "center", "center", "left"], fills=_api_fill)
    if api_rows:
        ws.auto_filter.ref = f"A{header_row}:D{row - 1}"
        ws.freeze_panes = f"A{header_row + 1}"
    _finish(ws, [30, 8, 12, 76])

    # ---------------- 宿主机 ----------------
    ws = wb.create_sheet("宿主机")
    ws.sheet_properties.tabColor = "1F4E79"
    row = _ws_head(ws, "宿主机资源", f"容器 {cont.get('id', '?')} · 时区 {snap['tz']}", 2, st)
    res_rows = [
        ["load", " / ".join(f"{x:.2f}" for x in (host.get("load") or [])) or "-"],
        ["内存", f"{_human(mem_total - mem_avail)} / {_human(mem_total)}（{mem_pct}%）"],
        ["Swap", f"{_human((host.get('swap_total') or 0) - (host.get('swap_free') or 0))} / "
                 f"{_human(host.get('swap_total') or 0)}"],
        ["磁盘", f"{_human(disk.get('used', 0))} / {_human(disk.get('total', 0))}"
                 f"（{round(disk.get('pct', 0))}%），可用 {_human(disk.get('free', 0))}"],
        ["inode", f"使用 {round(disk.get('ipct', 0))}%，剩 {disk.get('ifree', '-')}"],
        ["宿主已运行", f"{round((host.get('uptime_s') or 0) / 3600)} 小时"],
        ["数据目录", f"{cont.get('mount', '-')} ← {cont.get('host_path', '-')}"
                     f"（{cont.get('fs', '-')}）"],
    ]
    for name, path in (snap.get("paths") or {}).items():
        if not path or not os.path.isdir(path):
            continue
        files, size = _dir_stats(path)
        res_rows.append([f"目录 {name}", f"{files} 文件 · {_human(size)} · {_mode(path)}"])
    _ws_table(ws, row, ["指标", "值"], res_rows, [22, 88], st)
    _finish(ws, [22, 88])

    # ---------------- 网络 ----------------
    ws = wb.create_sheet("网络")
    ws.sheet_properties.tabColor = "0F8A5F"
    row = _ws_head(ws, "网络实测",
                   f"{net.get('checked_at', '')} · 结论：{net.get('verdict', '-')}", 5, st)
    net_rows = []
    for item in net.get("tcp") or []:
        net_rows.append([f"{item['host']}:{item['port']}", "✅" if item["ok"] else "❌",
                         f"{item.get('dns_ms') or 0:.0f}", f"{item.get('connect_ms') or 0:.0f}",
                         item.get("error") or item.get("label", "")])
    for item in net.get("https") or []:
        note = (item.get("error") or f"HTTP {item.get('code')}")[:90]
        if item.get("note"):
            note = f"{note}（{item['note']}）"[:90]
        net_rows.append([item["url"], "✅" if item["ok"] else "❌", "-",
                         f"{item.get('ms') or 0:.0f}", note])
    proxy = net.get("proxy") or {}
    for item in proxy.get("endpoints") or []:
        net_rows.append([
            f"代理端点 · {item.get('source', '')} {item.get('host', '')}:{item.get('port', '')}",
            "✅" if item.get("open") else "❌", "-", "-",
            (item.get("note") or "") + ("" if item.get("open") else "（连不上）"),
        ])
    api = proxy.get("api") or {}
    if api.get("host"):
        net_rows.append([f"代理内核 API · {api['host']}:{api.get('port', 9090)}",
                         "✅" if api.get("open") else "❌", "-", "-",
                         f"HTTP {api.get('code')}（需鉴权=正常；只看不控）"
                         if api.get("open") else "未监听"])
    egress = proxy.get("egress") or {}
    if egress:
        net_rows.append([f"代理出网 · {egress.get('endpoint', '')}",
                         "✅" if egress.get("ok") else "❌", "-",
                         f"{egress.get('ms') or 0:.0f}",
                         (egress.get("error") or f"HTTP {egress.get('code')}")[:90]])
    if proxy.get("kernels"):
        net_rows.append(["代理内核进程", "✅", "-", "-", "、".join(proxy["kernels"])])
    net_rows.append(["代理 · 环境变量", "-", "-", "-", proxy.get("env") or "<未设置>"])
    net_rows.append(["代理 · AstrBot 配置", "-", "-", "-", proxy.get("config") or "<未设置>"])
    verdicts = net.get("verdicts") or {}
    for label, key in (("结论 · 国内", "domestic"), ("结论 · 海外", "overseas"),
                       ("结论 · 代理", "proxy")):
        value = verdicts.get("proxy_short") if key == "proxy" else None
        value = value or verdicts.get(key)
        if value:
            net_rows.append([label, "-", "-", "-", str(value)[:90]])
    dns = net.get("dns") or {}
    if dns.get("servers"):
        net_rows.append(["DNS 服务器", "-", "-", "-", "、".join(dns["servers"])])

    def _net_fill(ws_, r, values):
        cell = ws_.cell(row=r, column=2)
        if values[1] == "✅":
            cell.font = st["ok_font"]
        elif values[1] == "❌":
            cell.font, cell.fill = st["bad_font"], st["bad_fill"]

    _ws_table(ws, row, ["目标", "结果", "DNS(ms)", "耗时(ms)", "备注"], net_rows,
              [44, 8, 10, 10, 46], st,
              aligns=["left", "center", "center", "center", "left"], fills=_net_fill,
              wrap_cols=[5])
    _finish(ws, [44, 8, 10, 10, 46])

    # ---------------- 文件链路 ----------------
    ws = wb.create_sheet("文件链路")
    ws.sheet_properties.tabColor = "7F6000"
    row = _ws_head(ws, "共享目录与写入",
                   f"{shared.get('dir', '-')} · {shared.get('verdict', '-')}", 5, st)
    chain_rows = []
    for item in shared.get("chain") or []:
        chain_rows.append([
            item["path"],
            item.get("mode") or (f"<{item.get('error')}>" if item.get("error") else "-"),
            "是" if item.get("ox") else "否",
            "是" if item.get("or") else "否",
            "✅" if item.get("ok") else "❌",
        ])
    chain_rows.append(["探针目录", shared.get("dir_mode", "-"), "-", "-", "-"])
    chain_rows.append(["探针文件", shared.get("file_mode", "-"), "-", "-", "-"])
    chain_rows.append(["文件 other-read", "是" if shared.get("other_read") else "否", "-", "-", "-"])
    chain_rows.append(["剩余空间", _human(shared.get("free") or 0), "-", "-", "-"])
    chain_rows.append([
        "写入实测",
        ("可写 " + (write.get("file_mode") or "")) if write.get("writable")
        else ("不可写 " + (write.get("error") or "")),
        "-", "-", "✅" if write.get("writable") else "❌",
    ])
    chain_rows.append(["umask", write.get("umask") or "-", "-", "-", "-"])
    chain_rows.append(["写入目录", write.get("dir", "-"), "-", "-", "-"])

    def _chain_fill(ws_, r, values):
        cell = ws_.cell(row=r, column=5)
        if values[4] == "✅":
            cell.font = st["ok_font"]
        elif values[4] == "❌":
            cell.font, cell.fill = st["bad_font"], st["bad_fill"]

    _ws_table(ws, row, ["路径 / 项目", "mode / 结果", "others-x", "others-r", "可进"],
              chain_rows, [54, 30, 11, 11, 8], st,
              aligns=["left", "left", "center", "center", "center"], fills=_chain_fill)
    _finish(ws, [54, 30, 11, 11, 8])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _fmt_caps_protocol(text: str) -> str:
    if not text:
        return "未知协议端"
    parts = dict(re.findall(r"(\w+)=([^\s]+)", text))
    if parts.get("app_name"):
        return f"{parts['app_name']} {parts.get('app_version', '')} " \
               f"({parts.get('protocol_version', '')})".strip()
    return text


def _version_match(need: str):
    """判断当前 AstrBot 版本是否满足 metadata 的 astrbot_version 约束。"""
    try:
        from packaging.specifiers import SpecifierSet
        from packaging.version import Version

        import astrbot

        current = getattr(astrbot, "__version__", "")
        if not current:
            return None
        return Version(str(current).lstrip("vV")) in SpecifierSet(need)
    except Exception:
        return None


def _count_tables(db_path: str) -> dict:
    if not os.path.exists(db_path):
        return {}
    out = {}
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3)
        try:
            for table in ("platform_stats", "conversations", "cron_jobs", "preferences"):
                try:
                    out[table] = con.execute(f"select count(*) from {table}").fetchone()[0]
                except Exception:
                    continue
        finally:
            con.close()
    except Exception:
        return {}
    return out


def _brief(data, limit: int = 90) -> str:
    """把 API 返回压成一行摘要（不打印密钥原文）。"""
    if isinstance(data, dict):
        parts = []
        for key in ("user_id", "nickname", "app_name", "app_version", "protocol_version",
                    "online", "good", "group_name", "member_count", "message_id", "file_id",
                    "folder_id", "file_count", "url", "cookies", "token", "csrf_token"):
            if key in data:
                value = data[key]
                if key in ("cookies", "token", "csrf_token") and value:
                    parts.append(f"{key}=<已返回 len={len(str(value))}>")
                else:
                    parts.append(f"{key}={value}")
        if parts:
            return " ".join(parts)[:limit]
        return f"dict({len(data)} keys)"
    if isinstance(data, list):
        return f"list({len(data)})"
    text = str(data)
    return text[:limit]


def _self_id(event) -> str:
    """取机器人账号。注意 aiocqhttp 的 bot.self_id 是 partial（动态属性代理），
    不能直接 str()。"""
    try:
        getter = getattr(event, "get_self_id", None)
        if callable(getter):
            value = getter()
            if value:
                return str(value)
    except Exception:
        pass
    try:
        value = getattr(getattr(event, "message_obj", None), "self_id", "")
        if value:
            return str(value)
    except Exception:
        pass
    return ""


# ============================ 插件主体 ============================
@register(
    PLUGIN_NAME,
    "哦鲸鲸",
    "后台体检：/diag 一次看清 插件 / AstrBot / 协议端 / 宿主机 四层状态，"
    "内含共享目录对端可见性与群成员信息导出（Excel）；无第三方依赖",
    PLUGIN_VERSION,
)
class PathDoctor(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self._config = config or {}
        self._paths = _paths()
        self._scan_cache = {"at": 0.0, "items": [], "issues": []}
        self._snapshot_task = None
        if bool(self._cfg("auto_snapshot", False)):
            try:
                self._snapshot_task = asyncio.create_task(self._snapshot_loop())
            except Exception as e:  # noqa: BLE001
                logger.warning(f"path_doctor: 自检快照任务未能启动: {e}")

    async def terminate(self):
        task = getattr(self, "_snapshot_task", None)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                pass

    # ---------------- 配置 ----------------
    def _cfg(self, key, default=None):
        try:
            value = self._config.get(key, default)
        except Exception:
            return default
        return default if value is None else value

    def _admin_only(self) -> bool:
        return bool(self._cfg("admin_only", True))

    def _admin_list(self) -> set:
        raw = self._cfg("admin_list", "") or ""
        if isinstance(raw, (list, tuple)):
            return {str(x).strip() for x in raw if str(x).strip()}
        return {x.strip() for x in str(raw).replace("，", ",").split(",") if x.strip()}

    def _check_permission(self, event: AstrMessageEvent) -> tuple:
        if not self._admin_only():
            return True, ""
        admins = self._admin_list()
        sender = str(getattr(event, "get_sender_id", lambda: "")() or "")
        if not admins:
            return True, "（admin_list 为空，已放行；建议在配置里填管理员 QQ）"
        if sender in admins:
            return True, ""
        return False, "❌ 仅管理员可用（path_doctor 会暴露路径与进程信息，admin_only 默认开启）"

    def _send_dir(self) -> Path:
        raw = str(self._cfg("send_dir", "") or "").strip()
        base = Path(raw) if raw else Path(self._paths["temp"])
        return base if base.name == _EXPORT_DIR else base / _EXPORT_DIR

    # ---------------- 命令入口 ----------------
    @filter.command("diag")
    async def diag(self, event: AstrMessageEvent):
        allowed, err = self._check_permission(event)
        if not allowed:
            await self._send_text(event, err)
            return

        args = self._arg(event)
        low = args.lower()
        head = low.split(" ", 1)[0] if low else ""
        tail = args[len(head):].strip() if head else ""

        if args == "" or head in ("path", "overview", "all"):
            await self._send_text(event, self._overview())
        elif head in ("plugin", "plugins", "star"):
            await self._send_text(event, self._plugin_section(tail))
        elif head in ("astrbot", "framework", "core"):
            await self._send_text(event, await self._astrbot_section())
        elif head in ("proto", "snowluma", "onebot", "qq", "protocol"):
            await self._send_text(event, await self._proto_section(event, tail))
        elif head in ("host", "sys", "system", "machine"):
            await self._send_text(event, self._host_section())
        elif head in ("net", "network", "proxy"):
            await self._send_text(event, self._net_section())
        elif head in ("shared", "perm", "share"):
            await self._send_text(event, self._shared_test(tail))
        elif head in ("writetest", "write", "w"):
            await self._send_text(event, self._write_test(tail))
        elif head in ("startup", "check", "自检", "启动自检", "上线自检"):
            await self._send_text(event, await self._startup_section(event))
        elif head in ("groups", "grouplist", "群列表"):
            await self._send_text(event, await self._group_list_text(event))
        elif head in ("export", "导出"):
            await self._export_one(event, tail)
        elif head in ("exportall", "export_all", "导出所有"):
            await self._export_all(event, tail)
        elif head in ("report", "xlsx", "excel"):
            await self._diag_xlsx(event)
        elif head == "txt":
            await self._send_txt(event, await self._full_report(event))
        elif head in ("json", "snapshot_json"):
            await self._diag_json(event)
        elif head in ("md", "markdown"):
            await self._diag_md(event)
        elif head in ("caps", "capability", "matrix", "capabilities"):
            await self._diag_caps(event)
        elif head in ("pack", "bundle", "zip"):
            await self._diag_pack(event)
        elif head in ("log", "logs"):
            await self._send_text(event, self._log_section(tail))
        elif head in ("help", "?", "？", "帮助"):
            if tail:
                await self._send_text(event, self._help_detail(tail))
            else:
                await self._send_text(event, self._help())
        elif head in ("commands", "cmd", "conflicts"):
            await self._send_text(event, self._command_section())
        elif head in ("snapshot", "snap"):
            await self._send_text(event, self._write_snapshot())
        else:
            hint = ""
            if args:
                hint = (f"⚠️ 没认出来「{args}」这个子命令。\n"
                        f"   想看单个命令的用法：/diag help <子命令>（如 /diag help net）\n\n")
            await self._send_text(event, hint + self._help())

    @filter.command("群信息")
    async def group_info_alias(self, event: AstrMessageEvent):
        await self._send_text(event, await self._group_list_text(event))

    @filter.command("导出群数据")
    async def export_group_alias(self, event: AstrMessageEvent, args: str = ""):
        await self._export_one(event, args)

    @filter.command("导出所有群数据")
    async def export_all_alias(self, event: AstrMessageEvent):
        await self._export_all(event)

    def _arg(self, event: AstrMessageEvent) -> str:
        msg = (event.message_str or "").strip()
        for token in ("/diag", "diag"):
            if token in msg:
                msg = msg.split(token, 1)[1]
                break
        return msg.strip()

    def _help(self) -> str:
        return (
            f"🧭 path_doctor {PLUGIN_VERSION} · 后台体检\n"
            f"{'=' * 34}\n"
            "/diag                  体检总览（四层结论 + 问题清单）\n"
            "/diag startup          启动自检清单（含共享目录与网络）\n"
            "/diag plugin [名字|all]  插件体检（元数据/依赖/配置/加载）\n"
            "/diag astrbot           框架体检（版本/平台/事件循环/库表）\n"
            "/diag proto [群号]       协议端实测（OneBot API + 端口连接）\n"
            "/diag host              宿主机（负载/内存/磁盘/inode/挂载）\n"
            "/diag net               网络实测（国内/海外/代理三结论）\n"
            "/diag shared [dir]      共享目录对端可见性\n"
            "/diag writetest [dir]   写入实测\n"
            "/diag groups            机器人所在群列表\n"
            "/diag export [群号] [xlsx|json|csv]   导出群成员（默认带统计样式的 xlsx）\n"
            "/diag exportall [xlsx|json|csv]      导出全部群成员（管理员）\n"
            "/diag caps              协议端能力矩阵（逐 API 实测）\n"
            "/diag json              结构化快照（JSON，可复制/可给别的插件读）\n"
            "/diag md                Markdown 报告（可直接贴文档）\n"
            "/diag pack              一键归档 zip：体检报告.xlsx + snapshot.json + caps.json + manifest.json\n"
            "/diag log [n]           日志按插件聚合（需开 log_file_enable）\n"
            "/diag commands          命令重名扫描（跨插件静态检查）\n"
            "/diag snapshot          写一份自检快照到 plugin_data\n"
            "/diag report            体检报告（7 页 Excel）\n"
            "/diag txt               体检报告（纯文本）\n"
            "\n"
            "看单个命令的用法：/diag help <子命令>（例 /diag help net）\n"
            "兼容旧写法：path / perm / write / txt\n"
            "自检清单别名：check / 自检 / 启动自检 / 上线自检\n"
            "群信息别名：群信息、导出群数据 [群号]、导出所有群数据"
        )

    def _help_detail(self, keyword: str) -> str:
        """/diag help <子命令>：单个命令的用法 / 作用 / 示例 / 相关。"""
        raw = (keyword or "").strip()
        key = _DIAG_ALIASES.get(raw.lower(), raw.lower())
        if key in ("", "diag") and not raw:
            return self._help()
        detail = _DIAG_DETAIL.get(key)
        if not detail:
            known = "、".join(sorted(k for k in _DIAG_DETAIL if k))
            return (f"⚠️ 没找到子命令「{raw}」。\n"
                    f"   可用的有：{known}\n"
                    f"   完整命令表：/diag help")
        usage, what, example, related = detail
        lines = [f"📖 {usage}",
                 "",
                 f"   作用：{what}",
                 f"   示例：{example}"]
        if related:
            lines.append(f"   相关：{related}")
        lines.append("")
        lines.append("   完整命令表：/diag help")
        return "\n".join(lines)

    # ==================================================================
    # 一、插件体检
    # ==================================================================
    def _loaded_plugins(self) -> dict:
        """运行中的插件注册表：{目录名: 元数据} 与 {插件名: 元数据}。"""
        by_dir, by_name = {}, {}
        try:
            from astrbot.core.star.star_manager import star_registry  # type: ignore

            for star in list(star_registry):
                name = getattr(star, "name", None) or ""
                root = getattr(star, "root_dir_name", None) or ""
                info = {
                    "name": name,
                    "root": root,
                    "version": getattr(star, "version", "") or "",
                    "author": getattr(star, "author", "") or "",
                    "activated": getattr(star, "activated", True),
                }
                if root:
                    by_dir[root] = info
                if name:
                    by_name[name] = info
        except Exception as e:  # noqa: BLE001
            logger.debug(f"path_doctor: 读取 star_registry 失败: {e}")
        return {"by_dir": by_dir, "by_name": by_name}

    def _inactivated(self) -> set:
        """从偏好表读取被停用插件的模块路径。"""
        path = os.path.join(self._paths["data"], "data_v4.db")
        if not os.path.exists(path):
            return set()
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3)
            try:
                row = con.execute(
                    "select data from preferences where key='inactivated_plugins' limit 1"
                ).fetchone()
            finally:
                con.close()
            if not row or not row[0]:
                return set()
            data = json.loads(row[0])
            items = data.get("val") if isinstance(data, dict) else data
            return {str(x) for x in (items or [])}
        except Exception:
            return set()

    def _scan_plugins(self, force: bool = False) -> tuple:
        now = time.time()
        if not force and now - self._scan_cache["at"] < 5:
            return self._scan_cache["items"], self._scan_cache["issues"]

        root = Path(self._paths["plugins"])
        loaded = self._loaded_plugins()
        inactivated = self._inactivated()
        config_dir = Path(self._paths["config"])
        issues: list = []
        result: list = []

        if not root.is_dir():
            return [], [f"插件目录不存在：{root}"]

        for path in sorted(root.iterdir()):
            if not path.is_dir():
                continue
            item = {
                "dir": path.name,
                "path": str(path),
                "meta": {},
                "meta_file": "",
                "problems": [],
                "warns": [],
                "missing_deps": [],
            }
            meta_file = next((path / n for n in _META_NAMES if (path / n).exists()), None)
            if meta_file is None:
                item["warns"].append("没有 metadata.yaml（AstrBot 会忽略该目录，可考虑清理）")
            else:
                item["meta_file"] = meta_file.name
                item["meta"] = _parse_meta(meta_file)

            meta = item["meta"]
            plugin_name = str(meta.get("name") or "").strip()
            version = str(meta.get("version") or "").strip()

            try:
                st = os.stat(path)
                item["mode"] = _stat.S_IMODE(st.st_mode)
                item["uid"] = st.st_uid
            except Exception:
                item["mode"] = None
                item["uid"] = None
                item["problems"].append("无法 stat 目录")

            if item["mode"] is not None and not (item["mode"] & 0o004):
                item["warns"].append("目录 others 不可读（宿主普通用户 / 备份工具读不到）")
            if re.search(r"(\.bak|backup|_bak|bak_|副本|_old)", path.name, re.I):
                item["warns"].append("疑似备份目录，放在 plugins/ 里会被当成插件扫描")

            info = loaded["by_dir"].get(path.name)
            if info is None and plugin_name:
                info = loaded["by_name"].get(plugin_name)
            item["loaded"] = bool(info)
            item["loaded_info"] = info or {}
            if info is not None and not info.get("activated", True):
                item["warns"].append("已注册但未激活")

            disabled = any(
                m == f"{path.name}.main" or m.endswith(f".{path.name}.main") or f".{path.name}." in m
                for m in inactivated
            )
            item["disabled"] = disabled

            if meta_file is not None and not item["loaded"]:
                if disabled:
                    item["warns"].append("已在后台停用（inactivated）")
                else:
                    item["problems"].append("未加载（可能导入失败或未被扫描到）")


            # 版本漂移：metadata vs 代码
            item["code_version"] = ""
            main_py = path / "main.py"
            if main_py.exists():
                # 注意：带 BOM 的源码 Python 导入器能接受，compile() 不接受；用 utf-8-sig 读
                try:
                    if os.path.getsize(main_py) < 2_000_000:
                        source = main_py.read_text(encoding="utf-8-sig", errors="replace")
                        compile(source, str(main_py), "exec")
                except SyntaxError as e:
                    item["problems"].append(f"main.py 语法错误（第 {e.lineno} 行：{e.msg}）")
                except Exception:
                    pass
            if main_py.exists() and version:
                code = _read_text(main_py, 400000)
                found = set()
                for args in _call_args(code, "register"):
                    for match in re.findall(r'["\']v?(\d+(?:\.\d+){1,2})["\']', args):
                        found.add(match)
                for match in re.findall(
                    r"\b(?:PLUGIN_)?VERSION\s*=\s*[\"']v?(\d+(?:\.\d+){1,2})[\"']", code
                ):
                    found.add(match)
                clean_version = version.lstrip("vV")
                others = sorted(v for v in found if v != clean_version)
                if others:
                    item["code_version"] = ",".join(others[:3])
                    item["warns"].append(f"代码版本 {'/'.join(others[:3])} ≠ metadata {version}")
                else:
                    item["code_version"] = clean_version

            # 依赖：先按分发包元数据核对，再看 import 名（- 与 _ 都试）
            req_file = next(
                (path / n for n in ("requirements.txt", "requirements-dev.txt") if (path / n).exists()),
                None,
            )
            if req_file is not None:
                for name, spec in _requirements(req_file):
                    status = _req_status(name, spec)
                    if status == "missing":
                        item["missing_deps"].append(f"{name}{spec} 未安装")
                    elif status.startswith("mismatch:"):
                        item["warns"].append(f"依赖版本不符 {name}: {status.split(':', 1)[1]}")
                if item["missing_deps"]:
                    if item["loaded"]:
                        item["warns"].append(f"声明依赖有 {len(item['missing_deps'])} 个没装（已加载，可能仅部分功能不可用）")
                    else:
                        item["problems"].append(f"缺依赖 {len(item['missing_deps'])} 个")

            # 配置
            cfg_name = f"{path.name}_config.json"
            cfg_path = config_dir / cfg_name
            item["config_file"] = ""
            item["config_ok"] = None
            if cfg_path.exists():
                item["config_file"] = cfg_name
                data = _read_json(cfg_path)
                if data is None:
                    item["config_ok"] = False
                    item["problems"].append("配置文件不是合法 JSON")
                else:
                    item["config_ok"] = True
            elif plugin_name and (config_dir / f"{plugin_name}_config.json").exists():
                item["warns"].append(f"配置文件名用了 {plugin_name}_config.json（应为 {cfg_name}）")

            schema = path / "_conf_schema.json"
            if schema.exists():
                sch = _read_json(schema)
                if sch is None:
                    item["problems"].append("_conf_schema.json 不是合法 JSON")
                elif item["config_ok"] and isinstance(sch, dict):
                    data = _read_json(cfg_path) or {}
                    if isinstance(data, dict):
                        missing = [k for k in sch if k not in data]
                        if missing:
                            item["warns"].append(f"配置缺键 {len(missing)} 个（走默认值）")

            # 兼容性
            need = str(meta.get("astrbot_version") or "").strip()
            item["need_version"] = need
            if need:
                matched = _version_match(need)
                if matched is False:
                    item["problems"].append(
                        f"声明 astrbot_version={need}，当前 {self._astrbot_version()} 不满足"
                    )

            files, size = _dir_stats(path)
            item["files"] = files
            item["size"] = size
            item["pycache"] = (path / "__pycache__").is_dir()

            if item["problems"]:
                issues.append(f"{path.name}: {item['problems'][0]}")
            result.append(item)

        self._scan_cache = {"at": now, "items": result, "issues": issues}
        return result, issues

    def _astrbot_version(self) -> str:
        try:
            import astrbot

            return str(getattr(astrbot, "__version__", "?"))
        except Exception:
            return "?"

    def _plugin_section(self, keyword: str = "") -> str:
        items, _issues = self._scan_plugins()
        if not items:
            return "【插件体检】插件目录为空或不可读"

        keyword = (keyword or "").strip()
        if keyword and keyword.lower() not in ("all", "全部"):
            matched = [
                it for it in items
                if keyword in it["dir"].lower()
                or keyword in str(it["meta"].get("name", "")).lower()
            ]
            if not matched:
                return f"【插件体检】没有匹配「{keyword}」的插件（共 {len(items)} 个）"
            items = matched

        bad = [it for it in items if it["problems"]]
        warn = [it for it in items if it["warns"] and not it["problems"]]
        ok = [it for it in items if not it["problems"] and not it["warns"]]
        loaded_n = len([it for it in items if it["loaded"]])

        lines = [
            f"🔌 插件体检 · {len(items)} 个目录（已加载 {loaded_n}）",
            f"   正常 {len(ok)} · 警告 {len(warn)} · 异常 {len(bad)}",
        ]
        for it in bad + warn + ok:
            mark = "❌" if it["problems"] else ("⚠️" if it["warns"] else "✅")
            name = it["meta"].get("name") or "-"
            version = it["meta"].get("version") or "-"
            state = "已加载" if it["loaded"] else ("已停用" if it["disabled"] else "未加载")
            lines.append("")
            lines.append(f"{mark} {it['dir']}  {version}  [{state}]")
            lines.append(
                f"   name={name} author={it['meta'].get('author', '-')} "
                f"files={it['files']} size={_human(it['size'])}"
            )
            for problem in it["problems"]:
                lines.append(f"   ❌ {problem}")
            for warning in it["warns"]:
                lines.append(f"   ⚠️ {warning}")
            for dep in it["missing_deps"][:5]:
                lines.append(f"      - {dep}")
            if it["config_file"]:
                lines.append(f"   配置 {it['config_file']} {'✅' if it['config_ok'] else '❌'}")
            elif it["meta"]:
                lines.append("   配置 <无>（使用默认值）")
        return "\n".join(lines)

    # ==================================================================
    # 二、AstrBot 框架
    # ==================================================================
    def _astrbot_config(self) -> dict:
        data = _read_json(os.path.join(self._paths["data"], "cmd_config.json"))
        return data if isinstance(data, dict) else {}

    def _platform_rows(self) -> list:
        cfg = self._astrbot_config()
        ports = _tcp_ports()
        rows = []
        for item in cfg.get("platform") or []:
            if not isinstance(item, dict):
                continue
            try:
                port = int(item.get("ws_reverse_port") or item.get("port") or 0)
            except Exception:
                port = 0
            slot = ports.get(port, {})
            rows.append(
                {
                    "id": item.get("id", "?"),
                    "type": item.get("type", "?"),
                    "enable": bool(item.get("enable")),
                    "port": port,
                    "listen": bool(slot.get("listen")),
                    "estab": int(slot.get("estab", 0)),
                }
            )
        return rows

    async def _loop_lag(self) -> float:
        try:
            loop = asyncio.get_running_loop()
            start = loop.time()
            await asyncio.sleep(0.05)
            return max(0.0, (loop.time() - start - 0.05) * 1000)
        except Exception:
            return -1.0

    async def _astrbot_section(self) -> str:
        paths = self._paths
        version = self._astrbot_version()
        started = _proc_start(1)
        uptime = (time.time() - started) if started else None
        cfg = self._astrbot_config()
        lag = await self._loop_lag()

        lines = [f"🤖 AstrBot {version} · Python {sys.version.split()[0]}", ""]

        rss = ""
        threads = "?"
        try:
            for line in _read_text("/proc/1/status").splitlines():
                if line.startswith("VmRSS:"):
                    rss = line.split(":", 1)[1].strip()
                elif line.startswith("Threads:"):
                    threads = line.split(":", 1)[1].strip()
        except Exception:
            pass
        try:
            fds = len(os.listdir("/proc/1/fd"))
        except Exception:
            fds = "?"

        lines.append("【运行】")
        lines.append(f"   已运行 {_dur(uptime)} · 启动于 {_ts(started)} · 时区 {_tz_note()}")
        lines.append(f"   RSS {rss or '?'} · 线程 {threads} · 打开 fd {fds}")
        if lag >= 0:
            health = "正常" if lag < 50 else ("偏高" if lag < 200 else "严重阻塞")
            lines.append(f"   事件循环延迟 {lag:.1f}ms（{health}）")

        rows = self._platform_rows()
        lines.append("")
        lines.append("【平台适配器】")
        if not rows:
            lines.append("   未配置任何平台")
        for row in rows:
            if not row["enable"]:
                state = "⏸ 已禁用"
            elif row["estab"] > 0:
                state = f"✅ 在线（{row['estab']} 条连接）"
            elif row["listen"]:
                state = "⚠️ 监听中但无客户端连接"
            else:
                state = "❌ 端口未监听（适配器没起来）"
            lines.append(f"   {row['id']} [{row['type']}] :{row['port']} {state}")

        db = os.path.join(paths["data"], "data_v4.db")
        wal = db + "-wal"
        temp_files, temp_size = _dir_stats(paths["temp"])
        temp_quota = cfg.get("temp_dir_max_size")
        lines.append("")
        lines.append("【数据与配置】")
        lines.append(
            f"   cmd_config version={cfg.get('config_version', '?')} "
            f"log_level={cfg.get('log_level', '?')} wake_prefix={cfg.get('wake_prefix', '?')}"
        )
        dash = cfg.get("dashboard") or {}
        if isinstance(dash, dict):
            try:
                dport = int(dash.get("port") or 0)
            except Exception:
                dport = 0
            listening = bool(_tcp_ports().get(dport, {}).get("listen")) if dport else False
            lines.append(
                f"   WebUI {'开启' if dash.get('enable') else '关闭'} :{dport} "
                f"{'✅ 监听中' if listening else '⚠️ 未监听'}"
            )
        lines.append(
            f"   数据库 {_human(os.path.getsize(db)) if os.path.exists(db) else '缺失'}"
            + (f" · WAL {_human(os.path.getsize(wal))}" if os.path.exists(wal) else "")
        )
        lines.append(
            f"   临时目录 {temp_files} 文件 {_human(temp_size)}"
            + (f" · 配额 {temp_quota}MB" if temp_quota else "")
        )
        counts = _count_tables(db)
        if counts:
            lines.append("   库表 " + " ".join(f"{k}={v}" for k, v in counts.items()))
        if cfg.get("http_proxy"):
            lines.append(f"   http_proxy {cfg.get('http_proxy')}")
        admins = cfg.get("admins_id") or []
        if isinstance(admins, list):
            lines.append(f"   管理员 {len(admins)} 人：{', '.join(map(str, admins[:5])) or '-'}")

        lines.append("")
        lines.append("【日志】")
        log_enable = cfg.get("log_file_enable")
        log_path = cfg.get("log_file_path") or "logs/astrbot.log"
        full_log = log_path if os.path.isabs(log_path) else os.path.join(paths["data"], log_path)
        if log_enable and os.path.exists(full_log):
            tail = _read_text(full_log)[-200000:]
            errs = [ln for ln in tail.splitlines() if "ERRO" in ln or "ERROR" in ln]
            lines.append(f"   {log_path} {_human(os.path.getsize(full_log))} · 错误行 {len(errs)}")
            for ln in errs[-3:]:
                lines.append(f"     · {ln[-160:]}")
        elif log_enable:
            lines.append(f"   已开启文件日志但文件不存在：{full_log}")
        else:
            lines.append("   文件日志未开启（log_file_enable=false），只能看 docker logs")

        items, _ = self._scan_plugins()
        lines.append("")
        lines.append(
            f"【插件】目录 {len(items)} · 已加载 {len([i for i in items if i['loaded']])} · "
            f"异常 {len([i for i in items if i['problems']])}"
        )
        return "\n".join(lines)

    # ==================================================================
    # 三、协议端实测
    # ==================================================================
    async def _api(self, event: AstrMessageEvent, action: str, **params):
        bot = getattr(event, "bot", None)
        if bot is None or not hasattr(bot, "call_action"):
            raise RuntimeError("当前事件没有 aiocqhttp bot 对象")
        start = time.perf_counter()
        data = await asyncio.wait_for(bot.call_action(action, **params), timeout=12)
        return data, (time.perf_counter() - start) * 1000

    async def _proto_section(self, event: AstrMessageEvent, group_id: str = "") -> str:
        lines = ["🛰 协议端实测（OneBot v11）", ""]
        ports = _tcp_ports()
        rows = self._platform_rows()

        lines.append("【连接】")
        lines.append(
            f"   事件 bot 类型 {type(getattr(event, 'bot', None)).__name__} · "
            f"self_id={_self_id(event) or '?'}"
        )
        for row in rows:
            slot = ports.get(row["port"], {})
            lines.append(
                f"   :{row['port']} [{row['id']}] listen={slot.get('listen', False)} "
                f"estab={slot.get('estab', 0)}"
            )

        if not bool(self._cfg("probe_protocol", True)):
            lines.append("   （probe_protocol=false，已跳过 API 实测）")
            return "\n".join(lines)

        lines.append("")
        lines.append("【API 往返】")
        probes = [
            ("get_login_info", "机器人账号"),
            ("get_status", "在线状态"),
            ("get_version_info", "协议端版本"),
            ("get_group_list", "群列表"),
        ]
        slowest = 0.0
        for action, _label in probes:
            try:
                data, ms = await self._api(event, action)
                slowest = max(slowest, ms)
                if action == "get_login_info" and isinstance(data, dict):
                    detail = f"QQ {data.get('user_id')} / {data.get('nickname')}"
                elif action == "get_status" and isinstance(data, dict):
                    detail = f"online={data.get('online')} good={data.get('good')}"
                elif action == "get_version_info" and isinstance(data, dict):
                    detail = (
                        f"{data.get('app_name')} {data.get('app_version')} "
                        f"({data.get('protocol_version')})"
                    )
                elif action == "get_group_list":
                    groups = data if isinstance(data, list) else []
                    detail = f"{len(groups)} 个群"
                else:
                    detail = str(data)[:80]
                lines.append(f"   ✅ {action:18s} {ms:6.0f}ms  {detail}")
            except asyncio.TimeoutError:
                lines.append(f"   ❌ {action:18s} 超时（>12s）")
            except Exception as e:  # noqa: BLE001
                lines.append(f"   ❌ {action:18s} {type(e).__name__}: {e}")

        if slowest and slowest < 1500:
            verdict = "✅ 协议端响应正常"
        elif slowest:
            verdict = "⚠️ 协议端偏慢"
        else:
            verdict = "❌ 协议端无响应"
        lines.append(f"   结论：{verdict}（最慢 {slowest:.0f}ms）")

        target = (group_id or "").strip()
        if target.isdigit():
            lines.append("")
            lines.append(f"【群 {target} 抽样】")
            try:
                data, ms = await self._api(event, "get_group_info", group_id=int(target))
                if isinstance(data, dict):
                    lines.append(
                        f"   群信息 {ms:.0f}ms · {data.get('group_name')} 成员 {data.get('member_count')}"
                    )
            except Exception as e:  # noqa: BLE001
                lines.append(f"   ❌ get_group_info: {type(e).__name__}: {e}")
            try:
                data, ms = await self._api(event, "get_group_member_list", group_id=int(target))
                members = data if isinstance(data, list) else []
                lines.append(f"   成员列表 {ms:.0f}ms · {len(members)} 人")
                if members and isinstance(members[0], dict):
                    lines.append(f"   首条字段：{', '.join(list(members[0].keys())[:10])}")
            except Exception as e:  # noqa: BLE001
                lines.append(f"   ❌ get_group_member_list: {type(e).__name__}: {e}")
        return "\n".join(lines)

    # ==================================================================
    # 四、宿主机
    # ==================================================================
    def _host_section(self) -> str:
        lines = ["🖥 宿主机（容器内可见视角）", ""]
        paths = self._paths
        mount = _mount_of(paths["data"])
        container_id = os.environ.get("HOSTNAME", "") or _read_text("/etc/hostname").strip()

        lines.append("【容器】")
        lines.append(f"   hostname(容器 ID) {container_id[:12] or '?'}")
        if mount:
            lines.append(f"   数据目录 {mount['mnt']} ← {_mount_text(mount)}")
        else:
            lines.append("   数据目录挂载：未识别（可能不是 bind mount）")
        lines.append(f"   时区 {_tz_note()} · pid 命名空间隔离，看不到宿主其它进程")

        load = _loadavg()
        mem = _meminfo()
        host_up = _host_uptime()
        lines.append("")
        lines.append("【负载与内存】")
        if load:
            lines.append(f"   load {load[0]:.2f} {load[1]:.2f} {load[2]:.2f}（宿主全局）")
        if mem:
            total = mem.get("MemTotal", 0)
            avail = mem.get("MemAvailable", 0)
            swap_total = mem.get("SwapTotal", 0)
            swap_free = mem.get("SwapFree", 0)
            pct = (total - avail) / total * 100 if total else 0
            lines.append(
                f"   内存 {_human(total - avail)} / {_human(total)} 已用（{pct:.0f}%），"
                f"可用 {_human(avail)}"
            )
            if swap_total:
                lines.append(f"   Swap {_human(swap_total - swap_free)} / {_human(swap_total)}")
        if host_up:
            lines.append(f"   宿主已运行 {_dur(host_up)}")

        svfs = _statvfs(paths["data"])
        lines.append("")
        lines.append("【磁盘】")
        if svfs:
            warn = "⚠️" if svfs["pct"] > 85 else "✅"
            lines.append(
                f"   {warn} {_human(svfs['used'])} / {_human(svfs['total'])} 已用"
                f"（{svfs['pct']:.0f}%），可用 {_human(svfs['free'])}"
            )
            if svfs["inodes"]:
                iwarn = "⚠️" if svfs["ipct"] > 85 else "✅"
                lines.append(f"   {iwarn} inode 使用 {svfs['ipct']:.0f}%（剩 {svfs['ifree']}）")
        for name in ("temp", "plugin_data", "plugins", "data"):
            target = paths.get(name)
            if not target or not os.path.isdir(target):
                continue
            files, size = _dir_stats(target)
            lines.append(f"   {name:12s} {files:>6} 文件 {_human(size):>8}  {target}")

        lines.append("")
        lines.append("【压力（PSI）】")
        for key in ("cpu", "memory", "io"):
            text = _read_text(f"/sys/fs/cgroup/{key}.pressure") or _read_text(f"/proc/pressure/{key}")
            first = text.splitlines()[0].strip() if text else ""
            lines.append(f"   {key:7s} {first or '<无数据>'}")

        lines.append("")
        lines.append("【容器（宿主 docker）】")
        containers = self._docker_containers()
        if containers is None:
            lines.append("   容器内未挂载 /var/run/docker.sock，看不到其它容器。")
            lines.append("   协议端（snowluma 等）存活请用 /diag proto 判断；")
            lines.append("   或把 docker.sock 只读挂进容器后本段自动生效。")
        elif isinstance(containers, dict) and containers.get("error"):
            lines.append(f"   ❌ 读取 docker.sock 失败：{containers['error']}")
        else:
            rows = containers if isinstance(containers, list) else []
            running = sum(1 for c in rows if str(c.get("State", "")).lower() == "running")
            lines.append(f"   {len(rows)} 个容器，运行中 {running} 个：")
            for item in rows[:15]:
                names = ",".join(n.lstrip("/") for n in (item.get("Names") or []))
                state = str(item.get("State", "")).lower()
                mark = "✅" if state == "running" else ("⏸" if state == "exited" else "⚠️")
                status = str(item.get("Status", ""))
                lines.append(f"   {mark} {names:18s} {status}")
            if len(rows) > 15:
                lines.append(f"   ...（只显示 15 个，共 {len(rows)}）")
        return "\n".join(lines)

    # ==================================================================
    # 四点五、网络实测（这台机器的常见故障点：代理节点挂了）
    # ==================================================================
    def _net_probe(self) -> dict:
        """结构化网络探测结果（面板 / JSON / 文本共用）。"""
        import socket
        import urllib.request

        tcp = []
        reach = {}
        for host, port, label in (("github.com", 443, "海外"),
                                  ("www.baidu.com", 443, "国内"),
                                  ("1.1.1.1", 443, "公共 DNS")):
            row = {"label": label, "host": host, "port": port, "ok": False,
                   "dns_ms": None, "connect_ms": None, "error": ""}
            try:
                start = time.perf_counter()
                addr = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)[0][4][0]
                row["dns_ms"] = (time.perf_counter() - start) * 1000
                sock = socket.socket()
                sock.settimeout(4)
                started = time.perf_counter()
                code = sock.connect_ex((addr, port))
                row["connect_ms"] = (time.perf_counter() - started) * 1000
                sock.close()
                row["ok"] = code == 0
                if not row["ok"]:
                    row["error"] = f"errno {code}"
                row["addr"] = addr
                row["fake_ip"] = _is_fake_ip(addr)
            except Exception as e:  # noqa: BLE001
                row["error"] = f"{type(e).__name__}: {e}"
            reach[label] = row["ok"]
            tcp.append(row)

        https = []
        http_ok = {}
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        opener.addheaders = [("User-Agent", "path_doctor")]
        for url, label in (("https://www.baidu.com", "国内"),
                           ("https://github.com", "海外")):
            row = {"label": label, "url": url, "ok": False, "code": None,
                   "ms": None, "error": "", "attempts": 0, "note": ""}
            first_error = ""
            # 走宿主 TUN / 代理时这条链路实测 0.7s～6.2s 波动很大，
            # 单发 + 6s 超时会让结论在 ✅/❌ 之间抖（实测踩过），所以 8s × 最多 2 次。
            for attempt in (1, 2):
                row["attempts"] = attempt
                try:
                    started = time.perf_counter()
                    with opener.open(url, timeout=8) as resp:
                        row["code"] = resp.status
                    row["ms"] = (time.perf_counter() - started) * 1000
                    row["ok"] = 200 <= (row["code"] or 0) < 400
                    row["error"] = ""
                    if attempt > 1:
                        row["note"] = f"第 {attempt} 次成功（首次 {first_error}）"
                        row["error"] = row["note"]
                    break
                except Exception as e:  # noqa: BLE001
                    row["error"] = f"{type(e).__name__}: {str(e)[:90]}"
                    if attempt == 1:
                        first_error = row["error"]
                        time.sleep(0.4)
            http_ok[label] = row["ok"]
            https.append(row)

        domestic_ok = bool(reach.get("国内"))
        overseas_ok = bool(reach.get("海外"))
        https_ok = all(r["ok"] for r in https)
        fake_ip = any(r.get("fake_ip") for r in tcp)
        proxy = self._proxy_probe(overseas_ok)
        proxy_ok = proxy.get("level") == "ok"

        if not domestic_ok:
            verdict = "连国内都不通，先查 DNS / 路由 / 网卡"
            ok = False
        elif not overseas_ok:
            verdict = "国内可达、海外不通 —— 先看代理节点是否可用"
            ok = False
        elif not https_ok:
            bad = "/".join(r["label"] for r in https if not r["ok"])
            verdict = f"TCP 都通，但 {bad} 的 HTTPS 失败（重试后仍失败）—— 多半是代理 / TLS 层"
            ok = False
        else:
            verdict = "内外网均可达"
            ok = True

        if domestic_ok:
            domestic_verdict = "✅ 可达"
        else:
            domestic_verdict = "❌ 不通（先查 DNS / 路由 / 网卡）"
        if not overseas_ok:
            overseas_verdict = "❌ 直连不通（看代理结论）"
        elif fake_ip:
            overseas_verdict = "✅ 直连可达（DNS 为 fake-ip：出网已走宿主 TUN 代理）"
        else:
            overseas_verdict = "✅ 直连可达"

        return {
            "tcp": tcp,
            "https": https,
            "proxy": proxy,
            "verdicts": {
                "domestic": domestic_verdict,
                "overseas": overseas_verdict,
                "proxy": proxy.get("verdict", ""),
                "proxy_short": proxy.get("verdict_short", "") or proxy.get("verdict", ""),
            },
            "dns": {
                "servers": _dns_servers(),
                "fake_ip": fake_ip,
                "targets": [{"host": r["host"], "addr": r.get("addr", "")} for r in tcp],
            },
            "ok": ok,
            # usable = 联网真能用：国内通，且海外「直连或经代理」有一条通
            "usable": bool(domestic_ok and (overseas_ok or proxy_ok)),
            "verdict": verdict,
            "checked_at": _now(),
        }

    # ------------------------------------------------------------------
    # 代理平面探测（v2.5.0 补测）
    # ------------------------------------------------------------------
    def _proxy_probe(self, overseas_direct_ok: bool) -> dict:
        """代理平面探测 + 真实出网实测（只读，不读也不打印任何密钥）。

        四个平面：① 环境变量 / AstrBot http_proxy ② 容器内本机代理端口
        ③ 宿主网关侧的代理内核 API（VM 上 mihomo 是宿主进程，容器内看不到端口）
        ④ 容器内代理内核进程名。结论分 ok / warn / bad 三档。
        """
        env_raw = (os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
                   or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
                   or os.environ.get("ALL_PROXY") or os.environ.get("all_proxy") or "")
        cfg_raw = str(self._astrbot_config().get("http_proxy") or "")
        gateway = _route_gateway()
        result = {
            "env": _mask_proxy(env_raw),
            "config": _mask_proxy(cfg_raw),
            "gateway": gateway,
            "endpoints": [],
            "api": {"host": "", "port": _PROXY_API_PORT, "open": False, "code": None},
            "kernels": _proxy_kernels(),
            "test_url": _PROXY_TEST_URL,
            "active": None,
            "egress": None,
            "level": "info",
            "verdict": "",
            "probed": True,
        }
        if not bool(self._cfg("probe_net_proxy", True)):
            result.update(probed=False,
                          verdict="已关闭代理实测（probe_net_proxy=false）")
            return result

        # ① 内核管理口：本机与宿主网关各看一眼，只判「端口可达 + 要不要鉴权」
        for host in [h for h in ("127.0.0.1", gateway) if h]:
            opened = _tcp_open(host, _PROXY_API_PORT, 1.2)
            code = (_http_probe_code(f"http://{host}:{_PROXY_API_PORT}/version", 2.5)
                    if opened else None)
            result["api"] = {"host": host, "port": _PROXY_API_PORT,
                             "open": opened, "code": code}
            if opened:
                break

        # ② 出网代理候选：环境变量 / AstrBot 配置 / 本机端口 / 宿主网关
        candidates = []
        for source, raw in (("环境变量", env_raw), ("AstrBot 配置", cfg_raw)):
            parsed = _parse_proxy(raw)
            if parsed:
                candidates.append({"source": source, "scheme": parsed[0], "host": parsed[1],
                                   "port": parsed[2], "note": "配置来源"})
        for port, label in _PROXY_PORTS:
            candidates.append({"source": "本机端口", "scheme": "socks5h", "host": "127.0.0.1",
                               "port": port, "note": label})
        if gateway:
            candidates.append({"source": "宿主网关", "scheme": "http", "host": gateway,
                               "port": _PROXY_PORTS[0][0],
                               "note": "宿主代理混合口（宿主只监听回环时探不到，属正常）"})

        seen = set()
        for row in candidates:
            key = (row["host"], row["port"])
            if key in seen:
                continue
            seen.add(key)
            row["open"] = _tcp_open(row["host"], row["port"], 1.2)
            result["endpoints"].append(row)

        # ③ 挑第一个「端口开着」的端点做真实出网实测
        for row in result["endpoints"]:
            if not row["open"]:
                continue
            trials = [row["scheme"]]
            if row["source"] == "本机端口":
                # 混合口两种协议都试一遍，避免把 HTTP-only 代理误判成「出网失败」
                trials.append("http")
            best = None
            for scheme in trials:
                trial = {"endpoint": f"{scheme}://{row['host']}:{row['port']}",
                         "source": row["source"], "url": _PROXY_TEST_URL,
                         "ok": False, "code": None, "ms": None, "error": ""}
                try:
                    code, ms = _proxy_https_get(scheme, row["host"], row["port"],
                                                _PROXY_TEST_URL, timeout=5.0)
                    trial.update(code=code, ms=ms, ok=bool(code and 200 <= code < 400))
                    if not trial["ok"]:
                        trial["error"] = f"HTTP {code}"
                except Exception as e:  # noqa: BLE001
                    trial["error"] = f"{type(e).__name__}: {str(e)[:80]}"
                best = trial
                if trial["ok"]:
                    break
            result["egress"] = best
            result["active"] = {"source": row["source"], "scheme": row["scheme"],
                                "host": row["host"], "port": row["port"]}
            break

        # ④ 结论：ok=可用 / warn=没配但先记着 / bad=配了却不通
        egress = result["egress"]
        api_open = bool(result["api"].get("open"))
        if egress and egress["ok"]:
            level = "ok"
            verdict = (f"代理可用（{egress['endpoint']} → {_PROXY_TEST_URL} "
                       f"{egress['code']} · {egress['ms']:.0f}ms）")
            short = (f"可用（{egress['endpoint']} → 海外 {egress['code']} · "
                     f"{egress['ms']:.0f}ms）")
            if not overseas_direct_ok:
                verdict += "；海外直连不通，出网依赖代理"
                short += "；海外直连不通，出网依赖代理"
        elif egress:
            level = "bad"
            short = (f"端口在监听但出网失败（{egress['endpoint']}：{egress['error']}）"
                     "——先换节点 / 查订阅")
            verdict = "代理" + short
        elif env_raw or cfg_raw:
            level = "bad"
            short = "配置了但端口都连不上（代理没起来，或地址 / 端口写错）"
            verdict = "代理" + short
        elif api_open:
            level = "warn"
            short = (f"未发现可用出网端口；{result['api']['host']}:{_PROXY_API_PORT} 上"
                     f"有代理内核 API（HTTP {result['api'].get('code')}，需鉴权）"
                     "——出网可能走宿主 TUN")
            verdict = short
        else:
            level = "ok" if overseas_direct_ok else "warn"
            short = ("未使用：直连海外可达" if overseas_direct_ok
                     else "未使用：直连海外不通，联网类插件会全挂——建议配代理")
            verdict = ("未使用代理：" + short.split("：", 1)[1])
        if result["kernels"]:
            verdict += f"（容器内代理内核：{'、'.join(result['kernels'])}）"
        result.update(level=level, verdict=verdict, verdict_short=short)
        return result

    def _net_section(self) -> str:
        data = self._net_probe()
        proxy = data.get("proxy") or {}
        dns = data.get("dns") or {}
        lines = ["🌐 网络实测", "", "【TCP】"]
        for row in data["tcp"]:
            if row["ok"]:
                addr = f" · {row.get('addr')}" if row.get("addr") else ""
                lines.append(
                    f"   ✅ {row['label']:8s} {row['host']}:{row['port']} "
                    f"DNS {row['dns_ms']:.0f}ms · TCP {row['connect_ms']:.0f}ms{addr}"
                )
            else:
                lines.append(
                    f"   ❌ {row['label']:8s} {row['host']}:{row['port']} {row['error']}"
                )
        lines.append("")
        lines.append("【HTTPS】")
        for row in data["https"]:
            if row["ok"]:
                note = f" · {row['note']}" if row.get("note") else ""
                lines.append(f"   ✅ {row['label']:6s} {row['url']} "
                             f"{row['code']} · {row['ms']:.0f}ms{note}")
            else:
                lines.append(f"   ❌ {row['label']:6s} {row['url']} {row['error']}"
                             f"（试了 {row.get('attempts') or 1} 次）")
        lines.append("")
        lines.append("【DNS】")
        lines.append(f"   服务器 {'、'.join(dns.get('servers') or []) or '<未读到 resolv.conf>'}")
        if dns.get("fake_ip"):
            lines.append("   解析结果含 198.18.x.x（mihomo TUN fake-ip）：DNS 已被代理接管")
        lines.append("")
        lines.append("【代理】")
        lines.append(f"   环境变量 {proxy.get('env') or '<未设置>'}")
        lines.append(f"   AstrBot 配置 {proxy.get('config') or '<未设置>'}")
        if not proxy.get("probed", True):
            lines.append("   （probe_net_proxy=false，本轮未做端口与出网实测）")
        for row in proxy.get("endpoints") or []:
            mark = "✅" if row.get("open") else "❌"
            lines.append(f"   {mark} {row.get('source', ''):5s} {row.get('host')}:{row.get('port')}"
                         f"  {row.get('note', '')}")
        api = proxy.get("api") or {}
        if api.get("host"):
            if api.get("open"):
                lines.append(f"   ✅ 代理内核 API {api['host']}:{api['port']} "
                             f"HTTP {api.get('code')}（需鉴权属正常；只看不控、不读密钥）")
            else:
                lines.append(f"   ⚪ 代理内核 API {api['host']}:{api['port']} 未监听")
        if proxy.get("kernels"):
            lines.append(f"   容器内代理内核进程：{'、'.join(proxy['kernels'])}")
        egress = proxy.get("egress") or {}
        if egress:
            if egress.get("ok"):
                lines.append(f"   出网实测 ✅ {egress['endpoint']} → {egress['url']} "
                             f"{egress['code']} · {egress['ms']:.0f}ms")
            else:
                lines.append(f"   出网实测 ❌ {egress['endpoint']}：{egress['error']}")
        lines.append("")
        lines.append("【结论】")
        lines.append(f"   国内 {data['verdicts']['domestic']}")
        lines.append(f"   海外 {data['verdicts']['overseas']}")
        prox_mark = {"ok": "✅", "bad": "❌"}.get(proxy.get("level"), "⚠️")
        lines.append(f"   代理 {prox_mark} {proxy.get('verdict_short') or data['verdicts']['proxy']}")
        lines.append(f"   综合 {'✅' if data['ok'] else '❌'} {data['verdict']}"
                     f" · 联网可用性 {'✅ 可用' if data.get('usable') else '❌ 不可用'}")
        if not data.get("usable"):
            lines.append("   指引：三条结论逐个排 —— 国内不通查 DNS/路由；海外不通查代理订阅；")
            lines.append("         代理「端口在但出网失败」= 换节点 / 查订阅到期（宿主自查："
                         "curl -H 'Authorization: Bearer <secret>' "
                         "http://127.0.0.1:9090/proxies 看 now）")
        return "\n".join(lines)

    # ==================================================================
    # 四点六、启动自检清单（/diag startup）
    # ==================================================================
    async def _startup_section(self, event: AstrMessageEvent) -> str:
        """启动 / 上线自检清单：把「装完该验什么」做成一条命令。

        条目与顺序固定，② 共享目录（shared）与 ③ 网络（net）分别与
        `/diag shared`、`/diag net` **同源同判定**，不会出现两处结论不一致。
        """
        items = []  # (名称, 状态, 摘要, 对策)

        # ① 框架与协议端
        rows = self._platform_rows()
        online = [r for r in rows if r["estab"] > 0]
        detail = f"AstrBot {self._astrbot_version()} · 平台 {len(rows)} 个（在线 {len(online)}）"
        state = "✅" if (rows and online) else ("⚠️" if rows else "❌")
        fix = ""
        if rows and bool(self._cfg("probe_protocol", True)):
            try:
                _data, api_ms = await self._api(event, "get_login_info")
                detail += f" · API {api_ms:.0f}ms"
                state = "✅"
            except Exception as e:  # noqa: BLE001
                detail += f" · API 失败 {type(e).__name__}"
                state = "❌"
                fix = "协议端没连上：/diag proto 看端口 ESTABLISHED 与 API 往返"
        elif not rows:
            fix = "没有平台适配器：WebUI → 消息平台 里确认 aiocqhttp 已启用"
        elif state != "✅":
            fix = "适配器端口没有连接：看协议端容器是否在跑（/diag proto）"
        items.append(("框架与协议端", state, detail, fix))

        # ② 共享目录（与 /diag shared 同源）
        shared = self._shared_probe()
        items.append((
            "共享目录可见（shared）",
            "✅" if shared.get("ok") else "❌",
            f"{shared.get('dir', '-')} · {shared.get('dir_mode', '-')}/"
            f"{shared.get('file_mode', '-')} · {shared.get('verdict', '-')}",
            "" if shared.get("ok") else "发文件前必看：/diag shared 定位是哪一级 others 缺 x/r",
        ))

        # ③ 网络（与 /diag net 同源）
        net = self._net_probe()
        verdicts = net.get("verdicts") or {}
        net_ok = bool(net.get("usable"))
        net_detail = (f"国内 {verdicts.get('domestic', '-')} · "
                      f"海外 {verdicts.get('overseas', '-')} · "
                      f"代理 {verdicts.get('proxy_short') or verdicts.get('proxy', '-')}")
        items.append((
            "网络（net）",
            "✅" if net_ok else "❌",
            net_detail,
            "" if net_ok else "联网类插件会一起挂：/diag net 看国内/海外/代理三条结论",
        ))

        # ④ 落盘可写（与 /diag writetest 同源）
        write = self._write_probe()
        items.append((
            "落盘可写",
            "✅" if write.get("writable") else "❌",
            f"{write.get('dir', '-')} · {write.get('file_mode') or write.get('error') or '-'}"
            f" · umask {write.get('umask') or '?'}",
            "" if write.get("writable") else "写不进共享目录：报告 / 导出 / 发文件都会失败",
        ))

        # ⑤ 插件健康
        scanned, _issues = self._scan_plugins()
        loaded = [i for i in scanned if i["loaded"]]
        bad = [i for i in scanned if i["problems"]]
        warns = [i for i in scanned if i["warns"] and not i["problems"]]
        state = "❌" if bad else ("⚠️" if warns else "✅")
        detail = (f"{len(scanned)} 个目录 · 已加载 {len(loaded)} · "
                  f"异常 {len(bad)} · 警告 {len(warns)}")
        if bad:
            detail += f"（{bad[0]['dir']} · {bad[0]['problems'][0]}）"
        items.append(("插件健康", state, detail,
                      "" if not bad else "逐个看：/diag plugin（未加载 / 缺依赖 / 配置坏）"))

        # ⑥ 关键配置
        admin_only = bool(self._cfg("admin_only", True))
        admins = sorted(self._admin_list())
        send_dir = str(self._cfg("send_dir", "") or "")
        shared_root = str(self._cfg("shared_dir", "") or self._paths["temp"]).rstrip("/")
        notes = [f"admin_only={'开' if admin_only else '关'}",
                 ("admin_list 已配 %d 人" % len(admins)) if admins
                 else "admin_list 为空（等于不限制）"]
        notes.append(f"send_dir {'默认（共享目录内）' if not send_dir else send_dir}")
        cfg_fix = []
        state = "✅"
        if admin_only and not admins:
            state = "⚠️"
            cfg_fix.append("admin_list 为空 → /diag 任何人可调，建议填管理员 QQ")
        if send_dir and not send_dir.rstrip("/").startswith(shared_root):
            state = "⚠️"
            cfg_fix.append("send_dir 不在共享目录内 → 发出的报告 / 导出会 EACCES")
        if not bool(self._cfg("probe_net_proxy", True)):
            cfg_fix.append("probe_net_proxy=false：/diag net 不做代理端口与出网实测")
        if not bool(self._cfg("probe_protocol", True)):
            cfg_fix.append("probe_protocol=false：/diag proto 不实测 API")
        items.append(("关键配置", state, " · ".join(notes),
                      "；".join(cfg_fix)))

        ok_n = sum(1 for _n, s, _d, _f in items if s == "✅")
        fail = [n for n, s, _d, _f in items if s == "❌"]
        warn = [n for n, s, _d, _f in items if s == "⚠️"]
        if fail:
            head = (f"❌ {ok_n}/{len(items)} 通过 · {len(fail)} 项必须先处理"
                    f"（{'、'.join(fail)}）")
        elif warn:
            head = f"⚠️ {ok_n}/{len(items)} 通过 · {len(warn)} 项注意（{'、'.join(warn)}）"
        else:
            head = f"✅ {ok_n}/{len(items)} 全通过（可以上线）"

        marks = "①②③④⑤⑥⑦⑧⑨"
        lines = [f"🧭 path_doctor {PLUGIN_VERSION} 启动自检清单",
                 f"   {_now()} · {_tz_note()}",
                 f"   结论：{head}",
                 ""]
        for idx, (name, state_, detail_, fix_) in enumerate(items):
            lines.append(f"{marks[idx]} {_pad_text(name, 24)}{state_} {detail_}")
            if fix_:
                lines.append(f"     → {fix_}")
        lines.append("")
        lines.append("   判据：② 与 /diag shared 同源、③ 与 /diag net 同源，两处结论永远一致。")
        lines.append("   排错顺序：③ 网络（联网插件全挂的根因）→ ② 共享目录（发文件 EACCES）"
                     "→ ① 协议端 → 其余")
        lines.append("   完整报告 /diag report（Excel） · 归档 /diag pack · 结构化 /diag json")
        return "\n".join(lines)

    # ==================================================================
    # 五、共享目录 / 写入
    # ==================================================================
    def _probe_chain(self, path: Path) -> list:
        parts = path.parts
        if not parts or parts[0] != "/":
            return [path]
        out = []
        for idx in range(1, len(parts) + 1):
            out.append(Path("/") if idx == 1 else Path("/" + "/".join(parts[1:idx])))
        return out

    def _shared_probe(self, target: str = "") -> dict:
        """共享目录可见性探测 → 结构化结果（文本/Excel/JSON 共用）。"""
        raw = (target or "").strip().strip('"').strip("'")
        base = Path(raw) if raw else Path(self._cfg("shared_dir") or self._paths["temp"])
        result = {
            "dir": str(base),
            "mount": _mount_of(str(base)),
            "probe": "",
            "dir_mode": "",
            "file_mode": "",
            "chain": [],
            "other_read": False,
            "free": None,
            "total": None,
            "broken": [],
            "ok": False,
            "verdict": "",
            "error": "",
        }
        probe_dir = base / _PROBE_DIR
        probe = probe_dir / f"probe_{int(time.time())}.txt"
        try:
            probe_dir.mkdir(parents=True, exist_ok=True)
            probe.write_text("path_doctor probe", encoding="utf-8")
            probe_dir.chmod(0o777)
            probe.chmod(0o666)
        except Exception as e:  # noqa: BLE001
            result["error"] = f"{type(e).__name__}: {e}"
            result["verdict"] = "创建探针失败"
            return result

        result["probe"] = str(probe)
        result["dir_mode"] = _mode(probe_dir)
        result["file_mode"] = _mode(probe)

        for sub in self._probe_chain(probe_dir):
            row = {"path": str(sub), "mode": "", "uid": "", "ox": False,
                   "or": False, "ok": False, "error": ""}
            try:
                st = os.stat(sub)
                mode = _stat.S_IMODE(st.st_mode)
                row.update(
                    mode=_stat.filemode(st.st_mode),
                    uid=st.st_uid,
                    ox=bool(mode & 0o001),
                    or_=bool(mode & 0o004),
                )
                row["or"] = bool(mode & 0o004)
                row["ok"] = row["ox"]
                if not row["ox"]:
                    result["broken"].append(str(sub))
            except Exception as e:  # noqa: BLE001
                row["error"] = type(e).__name__
                result["broken"].append(str(sub))
            result["chain"].append(row)

        try:
            result["other_read"] = bool(os.stat(probe).st_mode & 0o004)
        except Exception:
            result["other_read"] = False

        svfs = _statvfs(base)
        if svfs:
            result["free"] = svfs["free"]
            result["total"] = svfs["total"]

        if not result["broken"] and result["other_read"]:
            result["ok"] = True
            result["verdict"] = "others 可进可读（协议端 realpath + 读文件应成功）"
        elif result["broken"]:
            result["verdict"] = "有目录 others 缺 x，协议端进不去：" + ", ".join(result["broken"])
        else:
            result["verdict"] = "探针文件 others 不可读，协议端会 EACCES"

        try:
            probe.unlink(missing_ok=True)
            probe_dir.rmdir()
            result["cleaned"] = True
        except OSError:
            result["cleaned"] = False
        return result

    def _shared_test(self, target: str = "") -> str:
        data = self._shared_probe(target)
        lines = [f"📤 共享目录对端可见性 · {data['dir']}"]
        if data["mount"]:
            lines.append(f"   挂载 {data['mount']['mnt']} ← {_mount_text(data['mount'])}")
        if data["error"]:
            lines.append(f"   ❌ 创建探针失败: {data['error']}")
            return "\n".join(lines)

        lines.append(f"   探针 {data['probe']}")
        lines.append(f"   目录 {data['dir_mode']} · 文件 {data['file_mode']}")
        lines.append("   逐级权限（others 需 x 才能进、r 才能列）:")
        for row in data["chain"]:
            if row["error"]:
                lines.append(f"     {row['path']:42s} <{row['error']}>")
                continue
            mark = "✅" if row["ox"] else "❌ 缺 other-x"
            lines.append(
                f"     {row['path']:42s} {row['mode']} uid={row['uid']} "
                f"ox={'是' if row['ox'] else '否'} or={'是' if row['or'] else '否'} {mark}"
            )
        lines.append(f"   探针 other-read: {'是' if data['other_read'] else '否'}")
        if data["free"] is not None:
            lines.append(f"   剩余空间 {_human(data['free'])} / {_human(data['total'])}")

        if data["ok"]:
            lines.append("   结论: ✅ " + data["verdict"])
        elif data["broken"]:
            lines.append("   结论: ❌ " + data["verdict"])
            lines.append("   对策: chmod o+x <目录>（只补必要层级，别整盘放开）")
        else:
            lines.append("   结论: ❌ " + data["verdict"])
            lines.append("   对策: chmod o+r <文件>")
        lines.append("   已清理探针" if data.get("cleaned") else "   （探针目录非空，未删除）")
        return "\n".join(lines)

    def _write_probe(self, target: str = "") -> dict:
        raw = (target or "").strip().strip('"').strip("'")
        path = Path(raw) if raw else Path(self._cfg("shared_dir") or self._paths["temp"])
        result = {
            "dir": str(path), "exists": path.exists(), "is_dir": path.is_dir(),
            "mode": _mode(path) if path.exists() else "",
            "umask": "", "writable": False, "file_mode": "", "file_name": "",
            "error": "", "note": "",
        }
        if not result["exists"] or not result["is_dir"]:
            result["error"] = "目录不存在" if not result["exists"] else "不是目录"
            return result
        try:
            old_umask = os.umask(0o022)
            os.umask(old_umask)
            result["umask"] = oct(old_umask)
        except Exception:
            pass

        test = path / f".diag_write_test_{int(time.time())}"
        try:
            test.write_text("diag", encoding="utf-8")
            st = test.stat()
            result.update(writable=True, file_mode=_stat.filemode(st.st_mode),
                          file_name=test.name)
            if not (st.st_mode & 0o004):
                result["note"] = "产物 others 不可读，协议端读取会 EACCES（需 chmod o+r）"
            test.unlink(missing_ok=True)
        except Exception as e:  # noqa: BLE001
            result["error"] = f"{type(e).__name__}: {e}"
        return result

    def _write_test(self, target: str = "") -> str:
        data = self._write_probe(target)
        lines = [f"✍️ 写入实测 · {data['dir']}"]
        if not data["exists"]:
            lines.append("   ❌ 目录不存在")
            return "\n".join(lines)
        if not data["is_dir"]:
            lines.append("   ❌ 不是目录")
            return "\n".join(lines)
        lines.append(f"   目录 {data['mode']}")
        if data["umask"]:
            lines.append(f"   当前 umask {data['umask']}（决定新文件默认权限）")
        if data["writable"]:
            lines.append(f"   ✅ 可写 {data['file_name']} mode={data['file_mode']}")
            if data["note"]:
                lines.append(f"   ⚠️ {data['note']}")
            lines.append("   已清理")
        else:
            lines.append(f"   ❌ 不可写 {data['error']}")
        return "\n".join(lines)

    # ==================================================================
    # 六、群信息（参考 astrbot_plugin_group_information）
    # ==================================================================
    async def _group_list_text(self, event: AstrMessageEvent) -> str:
        lines = ["👥 群列表"]
        try:
            data, ms = await self._api(event, "get_group_list")
        except Exception as e:  # noqa: BLE001
            return f"❌ 取群列表失败：{type(e).__name__}: {e}"
        groups = data if isinstance(data, list) else []
        if not groups:
            return "机器人当前不在任何群里（或协议端未返回群列表）"
        bot_id = _self_id(event)
        lines.append(f"   共 {len(groups)} 个群 · 拉取 {ms:.0f}ms")

        def _count(group):
            try:
                return int(group.get("member_count") or 0)
            except Exception:
                return 0

        for idx, group in enumerate(sorted(groups, key=_count, reverse=True)[:30], 1):
            gid = group.get("group_id")
            name = _clean(group.get("group_name", ""))
            count = group.get("member_count", "?")
            mark = " ⬅本群" if str(gid) == str(event.get_group_id()) else ""
            lines.append(f"   {idx:>2}. {gid}  {name}  {count} 人{mark}")
        if len(groups) > 30:
            lines.append(f"   ...（只显示前 30，共 {len(groups)}）")
        if bot_id:
            lines.append(f"   机器人账号 {bot_id}")
        return "\n".join(lines)

    async def _members_of(self, event: AstrMessageEvent, group_id: int) -> list:
        data, _ms = await self._api(event, "get_group_member_list", group_id=int(group_id))
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            inner = data.get("data")
            if isinstance(inner, list):
                return inner
        return []

    def _save_export(self, name: str, payload: bytes, suffix: str):
        folder = self._send_dir()
        folder.mkdir(parents=True, exist_ok=True)
        try:
            folder.chmod(0o777)
        except OSError:
            pass
        path = folder / f"{name}.{suffix}"
        path.write_bytes(payload)
        try:
            path.chmod(0o666)
        except OSError:
            pass
        self._cleanup_exports(folder)
        return path, _mode(path)

    def _cleanup_exports(self, folder: Path):
        try:
            keep = int(self._cfg("export_keep_files", 5) or 5)
        except Exception:
            keep = 5
        try:
            files = sorted(folder.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
        except Exception:
            return
        for old in files[keep:]:
            try:
                old.unlink()
            except OSError:
                pass

    async def _send_file(self, event: AstrMessageEvent, path: Path, caption: str):
        chain = MessageChain([
            Plain(text=caption),
            File(name=path.name, file=str(path)),
        ])
        await event.send(event.chain_result(chain.chain))

    async def _export_meta(self, event: AstrMessageEvent) -> dict:
        source = "OneBot v11 / aiocqhttp"
        try:
            data, _ = await self._api(event, "get_version_info")
            if isinstance(data, dict) and data.get("app_name"):
                source = f"{data.get('app_name')} {data.get('app_version', '')}".strip()
        except Exception:
            pass
        return {
            "generated": _now(),
            "source": source,
            "bot": _self_id(event) or "?",
            "version": PLUGIN_VERSION,
        }

    async def _export_one(self, event: AstrMessageEvent, args: str = ""):
        allowed, err = self._check_permission(event)
        if not allowed:
            await self._send_text(event, err)
            return
        gid = ""
        fmt = "xlsx"
        for token in str(args or "").split():
            low = token.lower()
            if low in ("json", "csv", "xlsx"):
                fmt = low
            elif token.isdigit():
                gid = token
        if not gid:
            gid = str(event.get_group_id() or "")
        if not gid:
            await self._send_text(event, "请在群里使用，或带上群号：/diag export 123456 [xlsx|json|csv]")
            return
        if not gid.isdigit():
            await self._send_text(event, "请输入有效的群号（纯数字）")
            return

        await self._send_text(event, f"⏳ 正在导出群 {gid} 的成员（{fmt}），请稍候…")
        try:
            members = await self._members_of(event, int(gid))
        except Exception as e:  # noqa: BLE001
            await self._send_text(event, f"❌ 拉取群成员失败：{type(e).__name__}: {e}")
            return
        if not members:
            await self._send_text(event, "⚠️ 没取到成员数据（协议端返回空，可能是权限或实现限制）")
            return

        try:
            limit = int(self._cfg("export_max_members", 5000) or 5000)
        except Exception:
            limit = 5000

        group_name = ""
        try:
            info, _ = await self._api(event, "get_group_info", group_id=int(gid))
            if isinstance(info, dict):
                group_name = str(info.get("group_name") or "")
        except Exception:
            pass

        records = [_member_record(m, gid, group_name) for m in members[:limit]]
        note = ""
        if len(members) > limit:
            note = f"超出导出上限，仅包含前 {limit} 人（共 {len(members)} 人）"
        meta = await self._export_meta(event)
        stats = _block_stats(records)

        block = {"gid": gid, "gname": group_name, "records": records, "note": note}
        name = f"群成员_{gid}_{len(records)}人_{datetime.now().strftime('%m%d_%H%M%S')}"
        payload, suffix = self._export_payload([block], fmt, meta)
        path, mode = self._save_export(name, payload, suffix)

        caption = (
            f"✅ 群 {gid}{('「' + group_name + '」') if group_name else ''} 共 {len(records)} 名成员\n"
            f"群主 {stats['owner']} · 管理员 {stats['admin']} · 近 7 天发言 {stats['active7']} · "
            f"30 天未发言 {stats['silent30']}\n"
            f"文件 {path.name}（{_human(path.stat().st_size)}，{mode}）"
        )
        if note:
            caption += f"\n⚠️ {note}"
        await self._send_file(event, path, caption)

    def _export_payload(self, blocks: list, fmt: str, meta: dict):
        """同一份成员数据 → xlsx / json / csv。json 供其它插件或脚本直接消费。"""
        if fmt == "json":
            data = {
                "schema": 1,
                "tool": f"path_doctor {PLUGIN_VERSION}",
                "generated": meta.get("generated"),
                "source": meta.get("source"),
                "bot": meta.get("bot"),
                "groups": [
                    {
                        "group_id": b["gid"],
                        "group_name": b["gname"],
                        "count": len(b["records"]),
                        "note": b.get("note", ""),
                        "members": [
                            {
                                "user_id": r["uid"],
                                "nickname": r["nick"],
                                "card": r["card"],
                                "role": r["role"],
                                "role_text": _ROLE_TEXT.get(r["role"], r["role"]),
                                "level": r["level"],
                                "title": r["title"],
                                "join_time": _iso(r["join"]),
                                "last_sent_time": _iso(r["last"]),
                                "silent_days": r["silent"],
                                "shut_up_until": _iso(r["shut"]),
                                "unfriendly": r["unfriendly"],
                            }
                            for r in b["records"]
                        ],
                    }
                    for b in blocks
                ],
            }
            return json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8"), "json"

        if fmt == "csv":
            cols = _EXPORT_COLUMNS if len(blocks) > 1 else _DETAIL_COLUMNS
            rows = [[c[0] for c in cols]]
            idx = 0
            for block in blocks:
                for rec in block["records"]:
                    idx += 1
                    data = _record_dict(idx, rec)
                    rows.append([data[name] for name, _w, _t in cols])
            return _write_csv(rows), "csv"

        try:
            return _write_member_workbook(blocks, meta), "xlsx"
        except Exception:
            cols = _EXPORT_COLUMNS if len(blocks) > 1 else _DETAIL_COLUMNS
            rows = [[c[0] for c in cols]]
            idx = 0
            for block in blocks:
                for rec in block["records"]:
                    idx += 1
                    data = _record_dict(idx, rec)
                    rows.append([data[name] for name, _w, _t in cols])
            return _write_csv(rows), "csv"

    async def _export_all(self, event: AstrMessageEvent, args: str = ""):
        allowed, err = self._check_permission(event)
        if not allowed:
            await self._send_text(event, err)
            return
        fmt = "xlsx"
        for token in str(args or "").split():
            low = token.lower()
            if low in ("json", "csv", "xlsx"):
                fmt = low
        await self._send_text(event, f"⏳ 正在导出全部群成员（{fmt}，大群可能要一两分钟）…")
        try:
            data, _ = await self._api(event, "get_group_list")
        except Exception as e:  # noqa: BLE001
            await self._send_text(event, f"❌ 取群列表失败：{type(e).__name__}: {e}")
            return
        groups = data if isinstance(data, list) else []
        if not groups:
            await self._send_text(event, "机器人当前不在任何群里")
            return

        try:
            limit = int(self._cfg("export_max_members", 5000) or 5000)
        except Exception:
            limit = 5000

        blocks = []
        total = 0
        for group in groups:
            gid = str(group.get("group_id"))
            gname = _clean(group.get("group_name", ""))
            record = {"gid": gid, "gname": gname, "records": [], "note": ""}
            try:
                members = await self._members_of(event, int(gid))
            except Exception as e:  # noqa: BLE001
                record["note"] = f"拉取失败：{type(e).__name__}"
                blocks.append(record)
                continue
            picked = members[:limit]
            record["records"] = [_member_record(m, gid, gname) for m in picked]
            if len(members) > limit:
                record["note"] = f"超出上限，仅含前 {limit} 人（共 {len(members)} 人）"
            total += len(picked)
            blocks.append(record)

        meta = await self._export_meta(event)
        name = f"全部群成员_{len(groups)}群_{total}人_{datetime.now().strftime('%m%d_%H%M%S')}"
        payload, suffix = self._export_payload(blocks, fmt, meta)

        path, mode = self._save_export(name, payload, suffix)
        await self._send_file(
            event,
            path,
            f"✅ 已导出 {len(groups)} 个群 / {total} 名成员\n"
            f"文件 {path.name}（{_human(path.stat().st_size)}，{mode}）",
        )

    # ==================================================================
    # 六点五、日志 / 命令冲突 / 自检快照 / docker socket
    # ==================================================================
    def _log_section(self, args: str = "") -> str:
        """日志排查：按插件聚合 ERROR / WARN，给出最近的报错样本。"""
        try:
            count = int((args or "200").strip() or 200)
        except Exception:
            count = 200
        count = max(20, min(count, 2000))

        cfg = self._astrbot_config()
        log_path = str(cfg.get("log_file_path") or "logs/astrbot.log")
        full = log_path if os.path.isabs(log_path) else os.path.join(self._paths["data"], log_path)
        lines = ["📜 日志排查", ""]

        if not cfg.get("log_file_enable"):
            lines.append("   文件日志未开启（log_file_enable=false），无法读取历史错误。")
            lines.append("   终端日志在容器外（docker logs astrbot），插件读不到。")
            lines.append("")
            lines.append("   打开方式（WebUI → 配置文件 → 系统）：")
            lines.append("   log_file_enable: true  ·  log_file_path: logs/astrbot.log")
            lines.append("   重启后 `/diag log` 就能按插件聚合统计。")
            alt = Path(self._paths["data"]) / "logs"
            if alt.is_dir():
                files = sorted(alt.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
                if files:
                    lines.append("")
                    lines.append("   现有日志文件：")
                    for f in files[:5]:
                        lines.append(f"     {f.name}  {_human(f.stat().st_size)}")
            return "\n".join(lines)

        if not os.path.exists(full):
            lines.append(f"   ❌ 日志文件不存在：{full}")
            return "\n".join(lines)

        text = _read_text(full)
        tail = text[-400000:]
        all_lines = tail.splitlines()[-count:]
        err_lines = [ln for ln in all_lines if "ERRO" in ln or "ERROR" in ln]
        warn_lines = [ln for ln in all_lines if "WARN" in ln]

        buckets: dict = {}
        for ln in err_lines + warn_lines:
            match = re.search(r"\[([a-zA-Z0-9_]+)\]", ln)
            key = match.group(1) if match else "core"
            slot = buckets.setdefault(key, {"err": 0, "warn": 0, "sample": ""})
            if "ERRO" in ln or "ERROR" in ln:
                slot["err"] += 1
                slot["sample"] = slot["sample"] or ln
            else:
                slot["warn"] += 1

        lines.append(f"   文件 {log_path}（{_human(os.path.getsize(full))}）· 取样最后 {len(all_lines)} 行")
        lines.append(f"   ERROR {len(err_lines)} 行 · WARN {len(warn_lines)} 行")
        if not buckets:
            lines.append("   ✅ 取样范围内没有 ERROR/WARN")
            return "\n".join(lines)

        lines.append("")
        lines.append("   按来源聚合（ERROR / WARN）：")
        for key, slot in sorted(buckets.items(), key=lambda kv: -kv[1]["err"]):
            lines.append(f"   {key:34s} ❌{slot['err']:<3d} ⚠️{slot['warn']:<3d}")
        lines.append("")
        lines.append("   最近 5 条 ERROR：")
        for ln in err_lines[-5:]:
            clean = re.sub(r"\x1b\[[0-9;]*m", "", ln)
            lines.append(f"     · {clean[-180:]}")
        return "\n".join(lines)

    def _scan_commands(self) -> list:
        """静态扫描各插件的 @filter.command / command_group，找出重名命令。"""
        root = Path(self._paths["plugins"])
        found = []
        if not root.is_dir():
            return found
        pattern = re.compile(
            r"@(?:filter\.)?(?:command|command_group)\(\s*[\"']([^\"']{1,40})[\"']"
        )
        for path in sorted(root.iterdir()):
            if not path.is_dir():
                continue
            names = set()
            for py in path.rglob("*.py"):
                if "__pycache__" in str(py):
                    continue
                try:
                    if py.stat().st_size > 500000:
                        continue
                    code = py.read_text(encoding="utf-8-sig", errors="replace")
                except Exception:
                    continue
                for match in pattern.findall(code):
                    names.add(match.strip())
            if names:
                found.append({"dir": path.name, "names": sorted(names)})
        return found

    def _command_section(self) -> str:
        items = self._scan_commands()
        if not items:
            return "⌨️ 命令扫描：没有找到 @filter.command 声明"

        owners: dict = {}
        for item in items:
            for name in item["names"]:
                owners.setdefault(name.lower(), []).append(item["dir"])
        conflicts = {k: v for k, v in owners.items() if len(set(v)) > 1}
        total = sum(len(v) for v in owners.values())

        lines = ["⌨️ 命令扫描（静态，按插件目录）"]
        lines.append(f"   {len(items)} 个插件声明了 {total} 个命令/命令组")
        if conflicts:
            lines.append("")
            lines.append(f"   ⚠️ 发现 {len(conflicts)} 个重名命令（后注册的会覆盖或冲突）：")
            for name, dirs in sorted(conflicts.items()):
                lines.append(f"     /{name}  ← " + ", ".join(sorted(set(dirs))))
        else:
            lines.append("   ✅ 没有重名命令")
        lines.append("")
        lines.append("   各插件命令数：")
        for item in sorted(items, key=lambda x: -len(x["names"]))[:20]:
            sample = "、".join(item["names"][:6])
            more = "" if len(item["names"]) <= 6 else f" 等 {len(item['names'])} 个"
            lines.append(f"   {item['dir']:36s} {len(item['names']):>3}  {sample}{more}")
        return "\n".join(lines)

    def _snapshot_dir(self) -> Path:
        try:
            base = Path(StarTools.get_data_dir(PLUGIN_NAME)) / "snapshots"
        except Exception:
            base = Path(self._paths["plugin_data"]) / PLUGIN_NAME / "snapshots"
        return base

    def _snapshot_text(self) -> str:
        """不需要 event 的体检快照：插件 / 框架 / 宿主机 / 网络 / 共享。"""
        parts = [
            f"🧭 path_doctor {PLUGIN_VERSION} 自检快照",
            f"生成 {_now()} · {_tz_note()}",
            "=" * 40,
            self._overview(in_report=True),
            "-" * 40,
            self._plugin_section("all"),
            "-" * 40,
            self._host_section(),
            "-" * 40,
            self._shared_test(""),
            "-" * 40,
            self._write_test(""),
            "=" * 40,
        ]
        return "\n".join(parts)

    def _write_snapshot(self) -> str:
        folder = self._snapshot_dir()
        try:
            folder.mkdir(parents=True, exist_ok=True)
            text = self._snapshot_text()
            path = folder / f"snapshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            path.write_text(text, encoding="utf-8")
            keep = int(self._cfg("report_keep", 7) or 7)
            files = sorted(folder.glob("snapshot_*.txt"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
            for old in files[keep:]:
                try:
                    old.unlink()
                except OSError:
                    pass
            return (f"🗂 自检快照已保存（{len(text)} 字）\n"
                    f"   {path}\n   保留最近 {keep} 份")
        except Exception as e:  # noqa: BLE001
            return f"❌ 快照失败：{type(e).__name__}: {e}"

    async def _snapshot_loop(self):
        """按 report_interval_hours 周期写快照（默认 24h），失败不影响主流程。"""
        try:
            interval = float(self._cfg("report_interval_hours", 24) or 24)
        except Exception:
            interval = 24.0
        interval = max(1.0, interval)
        while True:
            try:
                self._write_snapshot()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"path_doctor: 自检快照失败: {e}")
            await asyncio.sleep(interval * 3600)

    def _docker_containers(self):
        """只读 docker.sock：挂载了就列出全部容器，没挂载返回 None。"""
        sock_path = "/var/run/docker.sock"
        if not os.path.exists(sock_path):
            return None
        try:
            import socket as _socket

            client = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
            client.settimeout(3)
            client.connect(sock_path)
            client.sendall(
                b"GET /containers/json?all=1 HTTP/1.1\r\n"
                b"Host: docker\r\nAccept: application/json\r\n"
                b"Connection: close\r\n\r\n"
            )
            chunks = []
            while True:
                data = client.recv(65536)
                if not data:
                    break
                chunks.append(data)
            client.close()
            raw = b"".join(chunks)
            head, _, body = raw.partition(b"\r\n\r\n")
            if b" 200 " not in head.split(b"\r\n")[0]:
                return {"error": head.split(b"\r\n")[0].decode("latin-1", "replace")}
            return json.loads(body.decode("utf-8", "replace"))
        except Exception as e:  # noqa: BLE001
            return {"error": f"{type(e).__name__}: {e}"}

    # ==================================================================
    # 六点八、结构化输出：一份数据，多种视图（JSON / Markdown / WebUI 面板）
    # ==================================================================
    _FAST_PROBES = [
        ("get_login_info", {}),
        ("get_status", {}),
        ("get_version_info", {}),
        ("get_online_clients", {}),
        ("get_group_list", {}),
        ("get_friend_list", {}),
        ("can_send_image", {}),
        ("can_send_record", {}),
    ]
    _DEEP_PROBES = [
        ("get_csrf_token", {}),
        ("get_credentials", {"domain": "qun.qq.com"}),
        ("get_cookies", {"domain": "qun.qq.com"}),
    ]
    _GROUP_PROBES = [
        ("get_group_info", "group"),
        ("get_group_member_list", "group"),
        ("get_group_member_info", "group_user"),
        ("get_group_honor_info", "group_honor"),
        ("get_group_system_msg", "none"),
        ("get_group_root_files", "group"),
        ("get_group_files_by_folder", "group_folder"),
        ("get_group_file_system_info", "group"),
    ]

    async def _proto_result(self, event, action: str, timeout: float = 5.0, **params) -> dict:
        bot = getattr(event, "bot", None)
        row = {"action": action, "ok": False, "ms": 0.0, "retcode": None,
               "status": "error", "detail": "", "error": ""}
        if bot is None or not hasattr(bot, "call_action"):
            row["error"] = "无 aiocqhttp bot 对象"
            return row
        start = time.perf_counter()
        try:
            data = await asyncio.wait_for(bot.call_action(action, **params), timeout=timeout)
            row.update(ok=True, status="ok", ms=(time.perf_counter() - start) * 1000)
            row["detail"] = _brief(data)
            return row
        except asyncio.TimeoutError:
            row.update(status="timeout", ms=timeout * 1000, error=f"超时（>{timeout:.0f}s）")
            return row
        except Exception as e:  # noqa: BLE001
            row["ms"] = (time.perf_counter() - start) * 1000
            row["retcode"] = getattr(e, "retcode", None)
            msg = str(e) or type(e).__name__
            low = msg.lower()
            if (
                any(k in low for k in ("unknown action", "不支持", "not support",
                                       "not implemented", "unsupported", "未知"))
                or row["retcode"] in (1404, 404)
            ):
                row["status"] = "unsupported"
            row["error"] = msg[:200]
            return row

    def _probe_ids(self, event) -> tuple:
        if event is None:
            return "", ""
        gid = str(event.get_group_id() or "")
        uid = str(getattr(event, "get_sender_id", lambda: "")() or "")
        return gid, uid

    async def _caps_matrix(self, event, deep: bool = True) -> dict:
        """协议端能力矩阵：逐个只读 API 实测，标出支持/不支持/超时。"""
        gid, uid = self._probe_ids(event)
        probes = list(self._FAST_PROBES)
        if deep:
            probes += self._DEEP_PROBES
        if gid.isdigit():
            for action, kind in self._GROUP_PROBES:
                if kind == "group":
                    probes.append((action, {"group_id": int(gid)}))
                elif kind == "group_user" and uid.isdigit():
                    probes.append((action, {"group_id": int(gid), "user_id": int(uid)}))
                elif kind == "group_honor":
                    probes.append((action, {"group_id": int(gid), "type": "all"}))
                elif kind == "group_folder":
                    probes.append((action, {"group_id": int(gid), "folder_id": "/"}))
                else:
                    probes.append((action, {}))

        results = []
        ident = {}
        for action, params in probes:
            row = await self._proto_result(event, action, 5.0 if deep else 3.0, **params)
            if action == "get_version_info" and row["ok"]:
                ident = {"detail": row["detail"]}
            results.append(row)

        summary = {"ok": 0, "unsupported": 0, "error": 0, "timeout": 0}
        for row in results:
            summary[row["status"]] = summary.get(row["status"], 0) + 1
        return {
            "generated": _now(),
            "self_id": _self_id(event) if event else "",
            "group_id": gid,
            "user_id": uid,
            "protocol": ident.get("detail", ""),
            "summary": summary,
            "results": results,
        }

    @staticmethod
    def _fmt_protocol(text: str) -> str:
        """把 app_name=mock-onebot app_version=11 ... 压成人话。"""
        if not text:
            return "未知协议端"
        parts = dict(re.findall(r"(\w+)=([^\s]+)", text))
        if parts.get("app_name"):
            ver = parts.get("app_version", "")
            proto = parts.get("protocol_version", "")
            return f"{parts['app_name']} {ver} ({proto})".strip()
        return text

    def _caps_section(self, caps: dict) -> str:
        summary = caps["summary"]
        lines = [f"🧩 协议端能力矩阵 · {self._fmt_protocol(caps.get('protocol') or '')}"]
        lines.append(
            f"   群 {caps.get('group_id') or '-'} · 用户 {caps.get('user_id') or '-'} · "
            f"✅{summary['ok']} ⚠️{summary['unsupported']} ❌{summary['error']} ⏱{summary['timeout']}"
        )
        marks = {"ok": "✅", "unsupported": "⚠️", "error": "❌", "timeout": "⏱"}
        for row in caps["results"]:
            mark = marks.get(row["status"], "?")
            tail = row["detail"] if row["status"] == "ok" else (row["error"] or "")
            lines.append(f"   {mark} {row['action']:26s} {row['ms']:5.0f}ms  {tail[:70]}")
        lines.append("")
        lines.append("   说明：⚠️ = 协议端不支持该 API（不是故障）；❌ = 返回错误；⏱ = 超时。")
        return "\n".join(lines)

    async def _proto_data(self, event) -> dict:
        caps = await self._caps_matrix(event, deep=False)
        return {
            "self_id": caps["self_id"],
            "protocol": caps["protocol"],
            "group_id": caps["group_id"],
            "api": caps["results"],
            "summary": caps["summary"],
        }

    def _snapshot_dict(self, lag_ms: float = -1.0, proto: dict = None,
                       net: dict = None) -> dict:
        items, issues = self._scan_plugins(force=True)
        paths = self._paths
        cfg = self._astrbot_config()
        load = _loadavg()
        mem = _meminfo()
        svfs = _statvfs(paths["data"])
        started = _proc_start(1)
        temp_dir = paths["temp"]
        temp_files, temp_size = _dir_stats(temp_dir) if os.path.isdir(temp_dir) else (0, 0)
        mount = _mount_of(paths["data"])
        container_id = os.environ.get("HOSTNAME", "") or _read_text("/etc/hostname").strip()

        plugins = []
        for it in items:
            meta = it.get("meta") or {}
            plugins.append({
                "dir": it["dir"],
                "name": meta.get("name", ""),
                "version": meta.get("version", ""),
                "author": meta.get("author", ""),
                "loaded": bool(it["loaded"]),
                "disabled": bool(it.get("disabled")),
                "problems": list(it.get("problems") or []),
                "warnings": list(it.get("warns") or []),
                "config": it.get("config_file", ""),
                "files": it.get("files", 0),
                "bytes": it.get("size", 0),
            })

        snap = {
            "schema": 1,
            "tool": {"name": "path_doctor", "version": PLUGIN_VERSION},
            "generated": _now(),
            "tz": _tz_note(),
            "container": {
                "id": container_id[:12],
                "mount": mount["mnt"] if mount else "",
                "host_path": mount["root"] if mount else "",
                "fs": mount["fstype"] if mount else "",
            },
            "host": {
                "load": list(load) if load else [],
                "mem_total": mem.get("MemTotal", 0),
                "mem_available": mem.get("MemAvailable", 0),
                "swap_total": mem.get("SwapTotal", 0),
                "swap_free": mem.get("SwapFree", 0),
                "uptime_s": _host_uptime(),
                "disk": svfs,
                "dirs": {
                    "temp": {"files": temp_files, "bytes": temp_size},
                },
            },
            "astrbot": {
                "version": self._astrbot_version(),
                "python": sys.version.split()[0],
                "started_at": _ts(started),
                "uptime_s": (time.time() - started) if started else None,
                "loop_lag_ms": lag_ms,
                "config_version": cfg.get("config_version"),
                "log_level": cfg.get("log_level"),
                "log_file_enable": bool(cfg.get("log_file_enable")),
                "wake_prefix": cfg.get("wake_prefix"),
                "http_proxy": cfg.get("http_proxy") or "",
                "admins": cfg.get("admins_id") or [],
                "webui_port": (cfg.get("dashboard") or {}).get("port"),
            },
            "platforms": self._platform_rows(),
            "plugins": {
                "total": len(items),
                "loaded": len([i for i in items if i["loaded"]]),
                "problem_count": len([i for i in items if i["problems"]]),
                "warning_count": len([i for i in items if i["warns"] and not i["problems"]]),
                "issues": issues,
                "items": plugins,
            },
            "paths": paths,
        }
        if proto is not None:
            snap["protocol"] = proto
        if net is not None:
            snap["net"] = net
        return snap

    def _md_report(self, snap: dict) -> str:
        def _cell(value) -> str:
            text = str(value if value is not None else "")
            return text.replace("|", "\\|").replace("\n", " ")

        def _table(headers: list, rows: list) -> list:
            out = ["| " + " | ".join(headers) + " |",
                   "| " + " | ".join(["---"] * len(headers)) + " |"]
            for row in rows:
                out.append("| " + " | ".join(_cell(c) for c in row) + " |")
            return out

        host = snap.get("host") or {}
        mem_total = host.get("mem_total") or 0
        mem_avail = host.get("mem_available") or 0
        disk = host.get("disk") or {}
        lines = [
            f"# path_doctor 体检报告 {snap['tool']['version']}",
            "",
            f"- 生成：{snap['generated']}（{snap['tz']}）",
            f"- 容器：{snap['container']['id']} · 宿主路径 `{snap['container']['host_path'] or '-'}`",
            f"- AstrBot：{snap['astrbot']['version']} · Python {snap['astrbot']['python']}",
            f"- 负载：{host.get('load') or '-'} · 内存 "
            f"{round((mem_total - mem_avail) / mem_total * 100) if mem_total else '?'}% · "
            f"磁盘 {round(disk.get('pct', 0))}%",
            "",
            "## 概览",
            "",
        ]
        plugin_info = snap["plugins"]
        lines += _table(
            ["项", "值"],
            [
                ["插件目录", plugin_info["total"]],
                ["已加载", plugin_info["loaded"]],
                ["异常", plugin_info["problem_count"]],
                ["警告", plugin_info.get("warning_count", 0)],
                ["平台适配器", len(snap.get("platforms") or [])],
                ["事件循环延迟", f"{snap['astrbot'].get('loop_lag_ms', -1):.1f} ms"],
            ],
        )
        if snap["plugins"]["issues"]:
            lines += ["", "## 问题清单", ""]
            for idx, issue in enumerate(snap["plugins"]["issues"], 1):
                lines.append(f"{idx}. {issue}")

        lines += ["", "## 插件", ""]
        lines += _table(
            ["目录", "版本", "状态", "配置", "问题"],
            [
                [
                    item["dir"], item["version"] or "-",
                    "已加载" if item["loaded"] else ("已停用" if item["disabled"] else "未加载"),
                    "✅" if item["config"] else "-",
                    "；".join((item["problems"] or []) + (item["warnings"] or [])) or "-",
                ]
                for item in plugin_info["items"]
            ],
        )

        if snap.get("platforms"):
            lines += ["", "## 平台适配器", ""]
            lines += _table(
                ["ID", "类型", "端口", "监听", "连接"],
                [[p["id"], p["type"], p["port"], p["listen"], p["estab"]]
                 for p in snap["platforms"]],
            )

        proto = snap.get("protocol")
        if proto:
            lines += ["", f"## 协议端（{self._fmt_protocol(proto.get('protocol') or '')}）", ""]
            lines += _table(
                ["API", "状态", "耗时", "结果"],
                [[row["action"], row["status"], f"{row['ms']:.0f}ms",
                  row["detail"] or row["error"]] for row in proto["api"]],
            )

        net = snap.get("net")
        if net:
            lines += ["", "## 网络", ""]
            lines += _table(
                ["目标", "结果", "耗时"],
                [[(r.get("host") or r.get("url") or "-"), "✅" if r["ok"] else "❌",
                  f"{r['connect_ms']:.0f}ms" if r.get("connect_ms") is not None
                  else (f"{r['ms']:.0f}ms" if r.get("ms") else "-")]
                 for r in (net.get("tcp") or []) + (net.get("https") or [])],
            )
            proxy = net.get("proxy") or {}
            proxy_rows = []
            for item in proxy.get("endpoints") or []:
                proxy_rows.append([f"{item.get('source', '')} {item.get('host')}:{item.get('port')}",
                                   "✅" if item.get("open") else "❌", item.get("note", "")])
            api = proxy.get("api") or {}
            if api.get("host"):
                proxy_rows.append([f"内核 API {api['host']}:{api.get('port', 9090)}",
                                   "✅" if api.get("open") else "❌",
                                   f"HTTP {api.get('code')}（需鉴权=正常）"
                                   if api.get("open") else "未监听"])
            egress = proxy.get("egress") or {}
            if egress:
                proxy_rows.append([f"出网 {egress.get('endpoint', '')}",
                                   "✅" if egress.get("ok") else "❌",
                                   (f"HTTP {egress.get('code')} · {egress.get('ms', 0):.0f}ms"
                                    if egress.get("ok") else egress.get("error", ""))])
            if proxy.get("kernels"):
                proxy_rows.append(["内核进程", "✅", "、".join(proxy["kernels"])])
            proxy_rows.append(["环境变量", "-", proxy.get("env") or "<未设置>"])
            proxy_rows.append(["AstrBot 配置", "-", proxy.get("config") or "<未设置>"])
            if proxy_rows:
                lines += ["", "### 代理平面", ""]
                lines += _table(["端点 / 项目", "结果", "备注"], proxy_rows)
            verdicts = net.get("verdicts") or {}
            lines += ["", "### 三条结论", ""]
            lines += _table(["维度", "结论"],
                            [["国内", verdicts.get("domestic", "-")],
                             ["海外", verdicts.get("overseas", "-")],
                             ["代理", verdicts.get("proxy_short") or verdicts.get("proxy", "-")],
                             ["综合", f"{net.get('verdict')}（联网可用性：{'可用' if net.get('usable') else '不可用'}）"]])
            dns = net.get("dns") or {}
            if dns:
                lines += ["", f"DNS 服务器：{'、'.join(dns.get('servers') or []) or '-'}"]

        lines += ["", "## 路径与磁盘", ""]
        lines += _table(
            ["目录", "路径", "权限"],
            [[name, path, _mode(path)] for name, path in (snap.get("paths") or {}).items()],
        )
        return "\n".join(lines) + "\n"

    async def _collect(self, event: AstrMessageEvent, deep_caps: bool = True) -> dict:
        """一次性采集：快照 + 共享目录 + 写入 + 能力矩阵。"""
        lag = await self._loop_lag()
        caps = await self._caps_matrix(event, deep=deep_caps)
        net = self._net_probe()
        snap = self._snapshot_dict(lag_ms=lag, proto=None, net=net)
        snap["protocol"] = {
            "self_id": caps["self_id"],
            "protocol": caps["protocol"],
            "group_id": caps["group_id"],
            "api": caps["results"],
            "summary": caps["summary"],
        }
        return {
            "snap": snap,
            "caps": caps,
            "shared": self._shared_probe(""),
            "write": self._write_probe(""),
        }

    async def _diag_xlsx(self, event: AstrMessageEvent):
        """体检报告 → 多页 Excel（默认交付形态）。"""
        data = await self._collect(event)
        payload = _write_diag_workbook(data["snap"], data["shared"], data["write"], data["caps"])
        path, mode = self._save_export(
            f"体检报告_{datetime.now().strftime('%m%d_%H%M%S')}", payload, "xlsx"
        )
        await self._send_file(
            event,
            path,
            f"📊 体检报告已生成（7 页：总览 / 插件 / 框架 / 协议端 / 宿主机 / 网络 / 文件链路）\n"
            f"插件 {data['snap']['plugins']['total']} 个 · API {len(data['caps']['results'])} 项 · "
            f"结论 {data['snap']['plugins']['problem_count']} 异常\n"
            f"文件 {path.name}（{_human(len(payload))}，{mode}）",
        )

    async def _diag_json(self, event: AstrMessageEvent):
        data = await self._collect(event)
        payload = json.dumps(data["snap"], ensure_ascii=False, indent=1).encode("utf-8")
        path, mode = self._save_export(f"snapshot_{datetime.now().strftime('%m%d_%H%M%S')}",
                                       payload, "json")
        await self._send_file(
            event, path,
            f"🧾 结构化快照已生成（{len(data['snap']['plugins']['items'])} 个插件 · "
            f"{len(data['caps']['results'])} 个 API 探测）\n"
            f"文件 {path.name}（{_human(len(payload))}，{mode}）",
        )

    async def _diag_md(self, event: AstrMessageEvent):
        data = await self._collect(event)
        text = self._md_report(data["snap"])
        path, mode = self._save_export(f"report_{datetime.now().strftime('%m%d_%H%M%S')}",
                                       text.encode("utf-8"), "md")
        await self._send_file(
            event, path,
            f"📝 Markdown 报告已生成（{len(text)} 字）\n文件 {path.name}（{mode}）",
        )

    async def _diag_caps(self, event: AstrMessageEvent):
        caps = await self._caps_matrix(event, deep=True)
        await self._send_text(event, self._caps_section(caps))
        payload = json.dumps(caps, ensure_ascii=False, indent=1).encode("utf-8")
        path, mode = self._save_export(f"caps_{datetime.now().strftime('%m%d_%H%M%S')}",
                                       payload, "json")
        await self._send_file(event, path, f"📎 能力矩阵已存档 {path.name}（{_human(len(payload))}）")

    # ---- 打包：一条命令出一个归档 ----
    async def _diag_pack(self, event: AstrMessageEvent):
        """把 Excel 报告 + 结构化数据打成一个 zip，便于归档与转交。"""
        import hashlib
        import zipfile

        data = await self._collect(event)
        snap, caps = data["snap"], data["caps"]
        xlsx = _write_diag_workbook(snap, data["shared"], data["write"], caps)

        files = {
            f"体检报告_{snap['generated'][:10].replace('-', '')}.xlsx": xlsx,
            "snapshot.json": json.dumps(snap, ensure_ascii=False, indent=1).encode("utf-8"),
            "caps.json": json.dumps(caps, ensure_ascii=False, indent=1).encode("utf-8"),
        }
        manifest = {
            "tool": f"path_doctor {PLUGIN_VERSION}",
            "generated": _now(),
            "environment": {
                "container": snap["container"]["id"],
                "astrbot": snap["astrbot"]["version"],
                "protocol": self._fmt_protocol(caps.get("protocol") or ""),
                "platforms": len(snap.get("platforms") or []),
                "plugins": snap["plugins"]["total"],
            },
            "files": [
                {
                    "name": name,
                    "bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "desc": desc,
                }
                for name, payload, desc in (
                    (next(k for k in files if k.endswith(".xlsx")), xlsx,
                     "体检报告（7 页 Excel：总览/插件/框架/协议端/宿主机/网络/文件链路）"),
                    ("snapshot.json", files["snapshot.json"], "机器读的结构化快照"),
                    ("caps.json", files["caps.json"], "协议端能力矩阵"),
                )
            ],
        }
        files["manifest.json"] = json.dumps(manifest, ensure_ascii=False, indent=1).encode("utf-8")

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, payload in files.items():
                zf.writestr(name, payload)
        payload = buf.getvalue()
        path, mode = self._save_export(
            f"diag_pack_{datetime.now().strftime('%m%d_%H%M%S')}", payload, "zip"
        )
        await self._send_file(
            event,
            path,
            f"📦 体检归档已生成（{len(files)} 个文件）\n"
            f"体检报告.xlsx + snapshot.json + caps.json + manifest.json\n"
            f"文件 {path.name}（{_human(len(payload))}，{mode}）",
        )

    # ==================================================================
    # 七、总览 & 完整报告
    # ==================================================================
    def _overview(self, in_report: bool = False) -> str:
        items, _ = self._scan_plugins()
        loaded = [i for i in items if i["loaded"]]
        bad = [i for i in items if i["problems"]]
        warns = [i for i in items if i["warns"] and not i["problems"]]
        rows = self._platform_rows()
        online = [r for r in rows if r["estab"] > 0]
        load = _loadavg()
        mem = _meminfo()
        svfs = _statvfs(self._paths["data"])
        temp = Path(self._paths["temp"])
        files, size = _dir_stats(temp) if temp.is_dir() else (0, 0)

        problems = []
        for it in bad:
            problems.append(f"{it['dir']} · {it['problems'][0]}")
        for it in warns[:5]:
            problems.append(f"{it['dir']} · {it['warns'][0]}")

        if problems:
            verdict = "⚠️ 发现 %d 个待处理项" % len(problems)
        else:
            verdict = "✅ 四层均正常"

        lines = [
            f"🧭 path_doctor {PLUGIN_VERSION} 体检总览",
            f"   {_now()} · {_tz_note()}",
            f"   结论：{verdict}",
            "",
            f"【插件】{len(items)} 个目录 · 已加载 {len(loaded)} · 异常 {len(bad)} · 警告 {len(warns)}",
            f"【框架】AstrBot {self._astrbot_version()} · 平台 {len(rows)} 个（在线 {len(online)}）· "
            f"WebUI :{(self._astrbot_config().get('dashboard') or {}).get('port', '?')}",
        ]

        if online:
            proto = "已连接"
        elif any(r["listen"] for r in rows):
            proto = "监听中但无客户端"
        else:
            proto = "未监听"
        port_text = ", ".join(
            f":{r['port']}{'✓' if r['estab'] else '✗'}" for r in rows
        ) or "-"
        lines.append(f"【协议端】{proto} · {port_text}")

        if load:
            host_line = f"【宿主机】load {load[0]:.2f}/{load[1]:.2f}/{load[2]:.2f}"
            if mem:
                total = mem.get("MemTotal", 0)
                avail = mem.get("MemAvailable", 0)
                host_line += f" · 内存 {(total - avail) / max(total, 1) * 100:.0f}%"
            if svfs:
                host_line += f" · 磁盘 {svfs['pct']:.0f}%"
            lines.append(host_line)

        lines.append(f"【共享】{temp} · {files} 文件 {_human(size)} · {_mode(temp)}")

        if problems:
            lines.append("")
            if in_report:
                lines.append("问题清单：")
            else:
                lines.append("问题清单（/diag report 看完整报告）：")
            for idx, text in enumerate(problems[:10], 1):
                lines.append(f"  {idx}. {text}")
            if len(problems) > 10:
                lines.append(f"  ...还有 {len(problems) - 10} 项")
        return "\n".join(lines)

    async def _full_report(self, event: AstrMessageEvent) -> str:
        parts = [
            f"🧭 path_doctor {PLUGIN_VERSION} 完整体检报告",
            f"生成 {_now()} · {_tz_note()}",
            "=" * 40,
            self._overview(in_report=True),
            "-" * 40,
            self._plugin_section("all"),
            "-" * 40,
            await self._astrbot_section(),
            "-" * 40,
            await self._proto_section(event),
            "-" * 40,
            self._host_section(),
            "-" * 40,
            self._command_section(),
            "-" * 40,
            self._log_section("200"),
            "-" * 40,
            self._shared_test(""),
            "-" * 40,
            self._write_test(""),
            "=" * 40,
            "（本报告只读采集；探针文件已清理）",
        ]
        return "\n".join(parts)

    # ==================================================================
    # 发送
    # ==================================================================
    async def _send_text(self, event: AstrMessageEvent, text: str):
        text = str(text or "")
        if len(text) <= _TEXT_LIMIT:
            await event.send(event.plain_result(text))
            return

        chunks = []
        current = ""
        for line in text.splitlines(keepends=True):
            while len(line) > _TEXT_LIMIT:
                if current:
                    chunks.append(current)
                    current = ""
                chunks.append(line[:_TEXT_LIMIT])
                line = line[_TEXT_LIMIT:]
            if len(current) + len(line) > _TEXT_LIMIT:
                chunks.append(current)
                current = line
            else:
                current += line
        if current:
            chunks.append(current)

        total = len(chunks)
        for idx, chunk in enumerate(chunks, 1):
            await event.send(event.plain_result(f"（{idx}/{total}）\n{chunk}"))
            await asyncio.sleep(0.3)

    async def _send_txt(self, event: AstrMessageEvent, text: str):
        folder = self._send_dir()
        folder.mkdir(parents=True, exist_ok=True)
        try:
            folder.chmod(0o777)
        except OSError:
            pass
        path = folder / f"diag_report_{datetime.now().strftime('%m%d_%H%M%S')}.txt"
        path.write_text(text, encoding="utf-8")
        try:
            path.chmod(0o666)
        except OSError:
            pass
        await self._send_file(
            event,
            path,
            f"📄 诊断报告已生成（{len(text)} 字）\n路径 {path}\n权限 {_mode(path)}",
        )
