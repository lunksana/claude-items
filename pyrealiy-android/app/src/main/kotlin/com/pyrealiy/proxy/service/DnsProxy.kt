package com.pyrealiy.proxy.service

import android.net.VpnService
import com.pyrealiy.proxy.core.Logger
import com.pyrealiy.proxy.core.ProxyTunnel
import com.pyrealiy.proxy.data.Profile
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.FileOutputStream
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.net.Socket
import java.nio.ByteBuffer

/**
 * DNS 查询拦截与转发
 *
 * 策略同 dns_forwarder.py：
 *   - 国内域名 → 直接 UDP 查询 localDns（如 223.5.5.5）
 *   - 境外域名 → DNS-over-TCP 经加密隧道查询 remoteDns（如 8.8.8.8）
 *
 * 简化版：此实现无路由规则区分，默认所有 DNS 均经隧道。
 * 如需分流可在此接入 DomainRouter。
 */
object DnsProxy {

    private const val TAG = "DnsProxy"
    private const val UDP_TIMEOUT_MS = 5000

    /**
     * 处理 TUN 截获的 UDP DNS 查询包，将响应写回 TUN
     */
    suspend fun handle(
        pkt: ByteArray,
        tunFd: FileOutputStream,
        profile: Profile,
        vpnService: VpnService,
    ) {
        val ihl = TunPacket.ipIHL(pkt)
        val srcIp   = TunPacket.srcIp(pkt)
        val dstIp   = TunPacket.dstIp(pkt)
        val srcPort = TunPacket.udpSrcPort(pkt, ihl)
        val dstPort = TunPacket.udpDstPort(pkt, ihl)
        val query   = TunPacket.udpPayload(pkt, ihl)

        if (dstPort != 53 || query.isEmpty()) return

        val domain = extractDomain(query)
        val response: ByteArray? = try {
            if (shouldUseTunnel(domain, profile.bypassCn)) {
                queryViaTunnel(query, profile, vpnService)
            } else {
                queryDirectUdp(query, profile.localDns, vpnService)
            }
        } catch (e: Exception) {
            Logger.w(TAG, "DNS failed for $domain: ${e.message}")
            nxDomain(query)
        }

        if (response != null) {
            val udpPkt = TunPacket.buildUdpPacket(
                srcIp = dstIp, dstIp = srcIp,
                srcPort = 53, dstPort = srcPort,
                payload = response,
            )
            try {
                synchronized(tunFd) { tunFd.write(udpPkt) }
            } catch (_: Exception) { /* TUN 已关，忽略 */ }
        }
    }

    // ── 路由判断（简单版：根据顶级域名） ───────────────────────────────────────

    private val CN_TLDS = setOf("cn", "com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn")

    private fun shouldUseTunnel(domain: String?, bypassCn: Boolean): Boolean {
        if (!bypassCn) return true                       // 关掉分流 → 所有 DNS 全走隧道
        if (domain == null) return true
        val parts = domain.split(".")
        if (parts.size >= 2) {
            val tld = parts.last()
            if (tld == "cn") return false
            if (parts.size >= 3 && "${parts[parts.size-2]}.$tld" in CN_TLDS) return false
        }
        return true  // 其他走隧道
    }

    // ── 直连 UDP DNS ────────────────────────────────────────────────────────────

    private suspend fun queryDirectUdp(query: ByteArray, dnsServer: String, vpnService: VpnService): ByteArray =
        withContext(Dispatchers.IO) {
            val sock = DatagramSocket()
            // 绕过自身 TUN，否则查询包又被 TUN 截获形成环路
            vpnService.protect(sock)
            sock.soTimeout = UDP_TIMEOUT_MS
            try {
                val addr = InetAddress.getByName(dnsServer)
                sock.send(DatagramPacket(query, query.size, addr, 53))
                val buf = ByteArray(4096)
                val resp = DatagramPacket(buf, buf.size)
                sock.receive(resp)
                buf.copyOf(resp.length)
            } finally {
                sock.close()
            }
        }

    // ── DNS-over-TCP via 加密隧道 ────────────────────────────────────────────

    private suspend fun queryViaTunnel(query: ByteArray, profile: Profile, vpnService: VpnService): ByteArray {
        val tunnel = ProxyTunnel(
            profile.serverHost, profile.serverPort, profile.password, profile.sni,
            protect = { s: Socket -> vpnService.protect(s) },
        )
        try {
            tunnel.connect(profile.remoteDns, 53)
            // DNS-over-TCP: 2-byte length prefix + query
            val lenPfx = ByteBuffer.allocate(2).putShort(query.size.toShort()).array()
            tunnel.send(lenPfx + query)

            // Read response: 2-byte length + response
            val buf = mutableListOf<Byte>()
            while (true) {
                val chunk = tunnel.recv()
                buf.addAll(chunk.toList())
                if (buf.size >= 2) {
                    val respLen = ((buf[0].toInt() and 0xFF) shl 8) or (buf[1].toInt() and 0xFF)
                    if (buf.size >= 2 + respLen) {
                        return buf.subList(2, 2 + respLen).toByteArray()
                    }
                }
            }
            @Suppress("UNREACHABLE_CODE")
            return byteArrayOf()
        } finally {
            tunnel.close()
        }
    }

    // ── DNS 报文工具 ─────────────────────────────────────────────────────────

    private fun extractDomain(query: ByteArray): String? {
        return try {
            var off = 12
            val labels = mutableListOf<String>()
            var hops = 0                                     // 防压缩指针环
            while (off < query.size && hops < 32) {
                val n = query[off].toInt() and 0xFF
                if (n == 0) break
                if (n and 0xC0 == 0xC0) {                    // 压缩指针：高 2 位为 11
                    if (off + 1 >= query.size) return null
                    off = ((n and 0x3F) shl 8) or (query[off + 1].toInt() and 0xFF)
                    hops++
                    continue
                }
                if (n and 0xC0 != 0) return null             // 保留位非法
                off++
                if (off + n > query.size) return null
                labels.add(query.copyOfRange(off, off + n).toString(Charsets.US_ASCII))
                off += n
            }
            if (labels.isEmpty()) null else labels.joinToString(".")
        } catch (_: Exception) { null }
    }

    /** 计算 question section 结束偏移；失败时回退到全长 */
    private fun questionEnd(query: ByteArray): Int {
        return try {
            val qd = ((query[4].toInt() and 0xFF) shl 8) or (query[5].toInt() and 0xFF)
            var pos = 12
            repeat(qd) {
                while (pos < query.size) {
                    val n = query[pos].toInt() and 0xFF
                    if (n == 0) { pos += 1; break }
                    if (n and 0xC0 != 0) { pos += 2; break }  // 指针（防御）
                    pos += 1 + n
                }
                pos += 4   // QTYPE + QCLASS
                if (pos > query.size) return query.size
            }
            pos
        } catch (_: Exception) { query.size }
    }

    private fun nxDomain(query: ByteArray): ByteArray {
        if (query.size < 12) return query
        val end = questionEnd(query)
        val h = query.copyOf(12)
        h[2] = (h[2].toInt() or 0x80).toByte()                  // 仅设 QR=1，保留 Opcode/RD 等
        h[3] = (h[3].toInt() and 0xF0 or 0x03).toByte()         // RCODE = 3 (NXDOMAIN)
        h[6] = 0; h[7] = 0; h[8] = 0; h[9] = 0; h[10] = 0; h[11] = 0
        // 只带 question 段；附带 EDNS0/OPT 等会让 header(ARCOUNT=0)与正文不一致
        return h + query.copyOfRange(12, end)
    }

    private operator fun ByteArray.plus(other: ByteArray): ByteArray {
        val r = ByteArray(size + other.size)
        copyInto(r); other.copyInto(r, size); return r
    }
}
