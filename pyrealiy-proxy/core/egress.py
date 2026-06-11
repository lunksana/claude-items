"""
出口（Egress）抽象 —— 决定 server 用什么"出口"去 dial 目标

设计动机：
  某些 VPS 的出口 IP 被 Netflix / ChatGPT / Google 等服务屏蔽。
  通过把那部分流量改走 WireGuard（如 Cloudflare WARP），借用 CF 出口 IP 池绕过。

机制：
  WireGuard 隧道本身由宿主机的 wg-quick 维护（不在 Python 里实现 WG 协议）。
  Server 用 SO_MARK 给 socket 打标，Linux 内核按 `ip rule fwmark X lookup table_Y`
  把流量路由进 WG 接口，对应用透明。

策略：
  - DefaultEgress：走系统默认路由（VPS 原生出口），现行行为
  - MarkedEgress(name, mark)：socket 上设 SO_MARK，触发策略路由
  - 失败时（无 CAP_NET_ADMIN）回落到 DefaultEgress 并 logger.warning
"""

from __future__ import annotations

import asyncio
import socket

from .utils import get_logger

logger = get_logger("egress")


class Egress:
    """出口基类"""
    name: str

    async def open_connection(
        self, host: str, port: int,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        raise NotImplementedError


class DefaultEgress(Egress):
    """OS 默认路由 —— pyrealiy 改造前的唯一行为"""

    name = "DIRECT"

    async def open_connection(self, host, port):
        return await asyncio.open_connection(host, port, limit=262144)


class MarkedEgress(Egress):
    """
    通过 SO_MARK + Linux 策略路由把流量送入特定路由表（典型用例：WireGuard 接口）。

    依赖宿主机预先设置好：
      ip rule add fwmark {mark} table {table}
      ip route add default dev {wg_iface} table {table}

    需用户手动配置 `ip rule` + `ip route` 把指定 fwmark 的流量送进 WireGuard
    路由表。参考 wg-quick + warp-cli 等工具的说明。

    重要：当前只针对 IPv4。如目标域名解析出 v6 地址会照样 setsockopt(SO_MARK)
    成功但内核找不到 v6 策略路由，连接会超时。
    解决：把宿主机或 server 端配置成 v4 优先，或者再加一条 `ip -6 rule`。
    """

    def __init__(self, name: str, mark: int):
        self.name = name
        self.mark = mark

    @staticmethod
    def probe(mark: int) -> tuple[bool, str]:
        """
        启动期一次性探测能否设置 SO_MARK（即是否有 CAP_NET_ADMIN）。
        返回 (ok, msg)。失败时 server 应当 fail loud，不要默默 fallback：
        用户会以为 WARP 在用结果服务器 IP 直接被 Netflix 拒。
        """
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_MARK, mark)
            return True, f"SO_MARK={mark:#x} 可用"
        except OSError as e:
            return False, f"SO_MARK={mark:#x} 设置失败：{e}（需 root 或 CAP_NET_ADMIN=ep）"
        finally:
            s.close()

    async def open_connection(self, host, port):
        loop = asyncio.get_event_loop()
        # **强制 v4**：典型的 WireGuard policy routing 只配 v4 路由表，如果用户主机偏好
        # v6（gai.conf 默认行为，IPv6 有 AAAA 时优先）会选出 v6 sockaddr，SO_MARK 设上但
        # 内核找不到 v6 路由表 → 连接默默走错出口或超时。强制 v4 让"目标只有 v6"的连接
        # 显式失败（gaierror），避免静默错误。需要 v6 WARP 的话用户自行加 `ip -6 rule`。
        infos = await loop.getaddrinfo(host, port, family=socket.AF_INET, type=socket.SOCK_STREAM)
        af, socktype, proto, _, sockaddr = infos[0]
        sock = socket.socket(af, socktype, proto)
        sock.setblocking(False)

        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_MARK, self.mark)
            await loop.sock_connect(sock, sockaddr)
        except BaseException:
            sock.close()
            raise
        return await asyncio.open_connection(sock=sock, limit=262144)
