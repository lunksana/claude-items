package com.pyrealiy.proxy.service

import java.net.Inet4Address
import java.nio.ByteBuffer

/**
 * IPv4 / TCP / UDP 报文解析与构造工具
 *
 * 只处理 IPv4，不支持 IPv6（VpnService.Builder 可按需关闭 IPv6 路由）。
 */
object TunPacket {

    const val PROTO_TCP = 6
    const val PROTO_UDP = 17

    // ── IP 头字段 ──────────────────────────────────────────────────────────────

    fun ipProto(pkt: ByteArray)  = pkt[9].toInt() and 0xFF
    fun ipIHL(pkt: ByteArray)    = (pkt[0].toInt() and 0x0F) * 4  // IP header 长度（字节）
    fun srcIp(pkt: ByteArray)    = pkt.copyOfRange(12, 16)
    fun dstIp(pkt: ByteArray)    = pkt.copyOfRange(16, 20)
    fun srcIpStr(pkt: ByteArray) = Inet4Address.getByAddress(srcIp(pkt)).hostAddress!!
    fun dstIpStr(pkt: ByteArray) = Inet4Address.getByAddress(dstIp(pkt)).hostAddress!!

    // ── TCP 头字段 ─────────────────────────────────────────────────────────────

    fun tcpSrcPort(pkt: ByteArray, ihl: Int) = u16(pkt, ihl)
    fun tcpDstPort(pkt: ByteArray, ihl: Int) = u16(pkt, ihl + 2)
    fun tcpSeq(pkt: ByteArray, ihl: Int)     = u32(pkt, ihl + 4)
    fun tcpAck(pkt: ByteArray, ihl: Int)     = u32(pkt, ihl + 8)
    fun tcpDataOff(pkt: ByteArray, ihl: Int) = ((pkt[ihl + 12].toInt() and 0xF0) ushr 4) * 4
    fun tcpFlags(pkt: ByteArray, ihl: Int)   = pkt[ihl + 13].toInt() and 0xFF
    fun tcpPayload(pkt: ByteArray, ihl: Int): ByteArray {
        val tcpHdrLen = tcpDataOff(pkt, ihl)
        return pkt.copyOfRange(ihl + tcpHdrLen, pkt.size)
    }

    const val FLAG_FIN = 0x01
    const val FLAG_SYN = 0x02
    const val FLAG_RST = 0x04
    const val FLAG_ACK = 0x10

    // ── UDP 头字段 ─────────────────────────────────────────────────────────────

    fun udpSrcPort(pkt: ByteArray, ihl: Int) = u16(pkt, ihl)
    fun udpDstPort(pkt: ByteArray, ihl: Int) = u16(pkt, ihl + 2)
    fun udpPayload(pkt: ByteArray, ihl: Int) = pkt.copyOfRange(ihl + 8, pkt.size)

    // ── 报文构造 ───────────────────────────────────────────────────────────────

    /**
     * 构造 IPv4 + TCP 报文（发往 TUN，即发给 app）
     * @param srcIp  源 IP（原连接的 dst）
     * @param dstIp  目标 IP（原连接的 src）
     * @param srcPort 源端口（原连接的 dst port）
     * @param dstPort 目标端口（原连接的 src port）
     */
    fun buildTcpPacket(
        srcIp: ByteArray, dstIp: ByteArray,
        srcPort: Int, dstPort: Int,
        seq: Long, ack: Long,
        flags: Int,
        windowSize: Int = 65535,
        payload: ByteArray = byteArrayOf(),
    ): ByteArray {
        val isSyn = (flags and FLAG_SYN) != 0
        val tcpHdrLen = if (isSyn) 24 else 20    // SYN(+ACK) 多带 4 字节 MSS option
        val tcpLen = tcpHdrLen + payload.size
        val ipLen  = 20 + tcpLen

        val buf = ByteBuffer.allocate(ipLen)

        // IP header
        buf.put(0x45.toByte())          // version=4, IHL=5 (20 bytes)
        buf.put(0x00)                   // DSCP/ECN
        buf.putShort(ipLen.toShort())
        buf.putShort(0)                 // ID
        // IP flags+fragoff：DF=1 必须放高字节的 bit6（0x4000 而不是 0x0040），
        // 否则会被解析为 fragoff=64 → 孤儿分片 → 内核静默丢弃 → 3WHS 永不完成
        buf.putShort(0x4000.toShort())  // flags: DF=1, fragoff=0
        buf.put(64)                     // TTL
        buf.put(PROTO_TCP.toByte())
        buf.putShort(0)                 // checksum (filled later)
        buf.put(srcIp)
        buf.put(dstIp)

        // TCP header
        buf.putShort(srcPort.toShort())
        buf.putShort(dstPort.toShort())
        buf.putInt(seq.toInt())
        buf.putInt(ack.toInt())
        buf.put(((tcpHdrLen / 4) shl 4).toByte())  // data offset
        buf.put(flags.toByte())
        buf.putShort(windowSize.toShort())
        buf.putShort(0)                 // TCP checksum (filled later)
        buf.putShort(0)                 // urgent pointer
        if (isSyn) {
            // MSS option: kind=2, length=4, value=1460 (MTU 1500 - IP20 - TCP20)
            buf.put(0x02); buf.put(0x04); buf.putShort(1460.toShort())
        }
        buf.put(payload)

        val pkt = buf.array()

        // Fill IP checksum
        putChecksum(pkt, 0, 20)

        // Fill TCP checksum (pseudo-header: srcIp, dstIp, 0, proto=6, tcpLen)
        val pseudo = ByteBuffer.allocate(12 + tcpLen)
        pseudo.put(srcIp); pseudo.put(dstIp)
        pseudo.put(0); pseudo.put(PROTO_TCP.toByte())
        pseudo.putShort(tcpLen.toShort())
        pseudo.put(pkt, 20, tcpLen)
        val tcpChecksum = checksum(pseudo.array())
        pkt[36] = (tcpChecksum ushr 8).toByte()
        pkt[37] = tcpChecksum.toByte()

        return pkt
    }

    /**
     * 构造 IPv4 + UDP 报文（用于 DNS 响应回写 TUN）
     */
    fun buildUdpPacket(
        srcIp: ByteArray, dstIp: ByteArray,
        srcPort: Int, dstPort: Int,
        payload: ByteArray,
    ): ByteArray {
        val udpLen = 8 + payload.size
        val ipLen  = 20 + udpLen
        val buf = ByteBuffer.allocate(ipLen)
        buf.put(0x45.toByte()); buf.put(0x00)
        buf.putShort(ipLen.toShort()); buf.putShort(0)
        buf.putShort(0x4000.toShort()); buf.put(64); buf.put(PROTO_UDP.toByte())   // DF=1, fragoff=0
        buf.putShort(0); buf.put(srcIp); buf.put(dstIp)
        buf.putShort(srcPort.toShort()); buf.putShort(dstPort.toShort())
        buf.putShort(udpLen.toShort()); buf.putShort(0)
        buf.put(payload)
        val pkt = buf.array()
        putChecksum(pkt, 0, 20)
        return pkt
    }

    // ── Checksum ──────────────────────────────────────────────────────────────

    private fun putChecksum(pkt: ByteArray, off: Int, hdrLen: Int) {
        pkt[off + 10] = 0; pkt[off + 11] = 0
        val cs = checksum(pkt.copyOfRange(off, off + hdrLen))
        pkt[off + 10] = (cs ushr 8).toByte()
        pkt[off + 11] = cs.toByte()
    }

    fun checksum(data: ByteArray): Int {
        var sum = 0L
        var i = 0
        while (i + 1 < data.size) {
            sum += ((data[i].toInt() and 0xFF) shl 8) or (data[i + 1].toInt() and 0xFF)
            i += 2
        }
        if (i < data.size) sum += (data[i].toInt() and 0xFF) shl 8
        while (sum ushr 16 != 0L) sum = (sum and 0xFFFF) + (sum ushr 16)
        return (sum.inv() and 0xFFFF).toInt()
    }

    // ── 工具 ──────────────────────────────────────────────────────────────────

    private fun u16(b: ByteArray, off: Int) =
        ((b[off].toInt() and 0xFF) shl 8) or (b[off + 1].toInt() and 0xFF)

    private fun u32(b: ByteArray, off: Int) =
        (((b[off].toLong() and 0xFF) shl 24) or
         ((b[off+1].toLong() and 0xFF) shl 16) or
         ((b[off+2].toLong() and 0xFF) shl 8) or
          (b[off+3].toLong() and 0xFF))
}
