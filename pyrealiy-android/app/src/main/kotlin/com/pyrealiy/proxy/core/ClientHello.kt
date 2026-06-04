package com.pyrealiy.proxy.core

import java.nio.ByteBuffer
import java.security.SecureRandom

/**
 * TLS ClientHello 构造器 — 同 tls_raw.py，三档指纹随机轮换。
 *
 * 返回 Pair(recordBytes, clientRandom)。
 * clientRandom 用于派生 ChaCha20-Poly1305 会话密钥。
 */
object ClientHello {

    private val rng = SecureRandom()

    private val GREASE = intArrayOf(
        0x0A0A, 0x1A1A, 0x2A2A, 0x3A3A, 0x4A4A, 0x5A5A, 0x6A6A, 0x7A7A,
        0x8A8A, 0x9A9A, 0xAAAA, 0xBABA, 0xCACA, 0xDADA, 0xEAEA, 0xFAFA,
    )

    fun build(serverName: String, sessionId: ByteArray): Pair<ByteArray, ByteArray> {
        require(sessionId.size == 32)
        val clientRandom = ByteArray(32).also { rng.nextBytes(it) }
        val sni = serverName.toByteArray()
        val gv = GREASE[rng.nextInt(GREASE.size)]

        val record = when (rng.nextInt(3)) {
            0    -> chrome(sni, sessionId, clientRandom, gv)
            1    -> firefox(sni, sessionId, clientRandom)
            else -> safari(sni, sessionId, clientRandom)
        }
        return Pair(record, clientRandom)
    }

    /** TLS 1.3 握手 flight：ChangeCipherSpec + 假 Finished */
    fun buildFakeClientTail(): ByteArray {
        val ccs = byteArrayOf(0x14, 0x03, 0x03, 0x00, 0x01, 0x01)
        val finBody = ByteArray(52).also { rng.nextBytes(it) }
        val finLen = ByteBuffer.allocate(2).putShort(finBody.size.toShort()).array()
        return ccs + byteArrayOf(0x17, 0x03, 0x03) + finLen + finBody
    }

    // ── 私有构建函数 ───────────────────────────────────────────────────────────

    private fun chrome(sni: ByteArray, sessionId: ByteArray, cr: ByteArray, gv: Int): ByteArray {
        val ciphers = shorts(gv, 0x1301, 0x1302, 0x1303, 0xC02B, 0xC02F, 0xC02C, 0xC030, 0x009C, 0x00FF)
        val groups  = shorts(gv, 0x001D, 0x0017, 0x0018)
        val sigalgs = shorts(0x0403, 0x0804, 0x0401, 0x0503, 0x0805, 0x0501, 0x0806, 0x0601, 0x0201)
        val exts = concat(
            ext(gv, byteArrayOf(0x00)),
            sniExt(sni),
            ext(0x0017, byteArrayOf()),
            ext(0xFF01, byteArrayOf(0x00)),
            ext(0x000A, u16Prefix(groups)),
            ext(0x000B, byteArrayOf(0x01, 0x00)),
            ext(0x0023, byteArrayOf()),
            ext(0x0005, byteArrayOf(0x01, 0x00, 0x00, 0x00, 0x00)),
            alpnExt(),
            ext(0x000D, u16Prefix(sigalgs)),
            keyShareExt(gv),
            ext(0x002D, byteArrayOf(0x01, 0x01)),
            ext(0x002B, byteArrayOf(0x04, 0x03, 0x04, 0x03, 0x03)),
            ext(gv, byteArrayOf(0x00)),
        )
        return assemble(sessionId, cr, ciphers, exts)
    }

    private fun firefox(sni: ByteArray, sessionId: ByteArray, cr: ByteArray): ByteArray {
        val ciphers = shorts(0x1301, 0x1303, 0x1302, 0xC02B, 0xC02F, 0xCCA9, 0xCCA8, 0xC02C, 0xC030, 0xC009, 0xC013, 0x009C)
        val groups  = shorts(0x001D, 0x0017, 0x0018, 0x0019, 0x0100, 0x0101)
        val sigalgs = shorts(0x0403, 0x0503, 0x0603, 0x0804, 0x0805, 0x0806, 0x0401, 0x0501, 0x0601, 0x0201, 0x0203)
        val exts = concat(
            sniExt(sni),
            ext(0x0017, byteArrayOf()),
            ext(0xFF01, byteArrayOf(0x00)),
            ext(0x000A, u16Prefix(groups)),
            ext(0x000B, byteArrayOf(0x01, 0x00)),
            ext(0x0023, byteArrayOf()),
            alpnExt(),
            ext(0x0005, byteArrayOf(0x01, 0x00, 0x00, 0x00, 0x00)),
            ext(0x000D, u16Prefix(sigalgs)),
            keyShareExt(null),
            ext(0x002D, byteArrayOf(0x01, 0x01)),
            ext(0x002B, byteArrayOf(0x04, 0x03, 0x04, 0x03, 0x03)),
            ext(0x001B, byteArrayOf(0x02, 0x00, 0x02)),
            ext(0x0031, byteArrayOf()),
            ext(0x001C, byteArrayOf(0x40.toByte(), 0x01)),
        )
        return assemble(sessionId, cr, ciphers, exts)
    }

    private fun safari(sni: ByteArray, sessionId: ByteArray, cr: ByteArray): ByteArray {
        val ciphers = shorts(0x1301, 0x1302, 0x1303, 0xC02C, 0xC02B, 0xC030, 0xC02F, 0xCCA9, 0xCCA8, 0xC024, 0xC023, 0xC00A, 0xC009, 0x009D, 0x009C)
        val groups  = shorts(0x001D, 0x0017, 0x0018, 0x0019)
        val sigalgs = shorts(0x0403, 0x0804, 0x0401, 0x0503, 0x0603, 0x0805, 0x0806, 0x0501, 0x0601, 0x0203, 0x0201)
        val exts = concat(
            ext(0xFF01, byteArrayOf(0x00)),
            sniExt(sni),
            ext(0x0017, byteArrayOf()),
            ext(0x0023, byteArrayOf()),
            ext(0x000D, u16Prefix(sigalgs)),
            ext(0x0005, byteArrayOf(0x01, 0x00, 0x00, 0x00, 0x00)),
            alpnExt(),
            ext(0x000A, u16Prefix(groups)),
            ext(0x000B, byteArrayOf(0x01, 0x00)),
            ext(0x001B, byteArrayOf(0x04, 0x00, 0x01, 0x00, 0x02)),
            ext(0x0031, byteArrayOf()),
            keyShareExt(null),
            ext(0x002D, byteArrayOf(0x01, 0x01)),
            ext(0x002B, byteArrayOf(0x04, 0x03, 0x04, 0x03, 0x03)),
        )
        return assemble(sessionId, cr, ciphers, exts)
    }

    // ── 低层工具 ───────────────────────────────────────────────────────────────

    private fun assemble(sessionId: ByteArray, cr: ByteArray, ciphers: ByteArray, exts: ByteArray): ByteArray {
        val helloBody = concat(
            byteArrayOf(0x03, 0x03),         // legacy_version
            cr,                               // client_random
            byteArrayOf(sessionId.size.toByte()) + sessionId,
            u16Prefix(ciphers),              // cipher suites
            byteArrayOf(0x01, 0x00),          // compression: null
            u16Prefix(exts),
        )
        val hsLen = helloBody.size
        val hs = byteArrayOf(0x01) +          // handshake type: ClientHello
                byteArrayOf(0, (hsLen ushr 16).toByte(), (hsLen ushr 8).toByte(), hsLen.toByte()).drop(1).toByteArray() +
                helloBody
        val recLen = hs.size
        return byteArrayOf(0x16, 0x03, 0x01, (recLen ushr 8).toByte(), recLen.toByte()) + hs
    }

    private fun ext(type: Int, data: ByteArray): ByteArray {
        val bb = ByteBuffer.allocate(4 + data.size)
        bb.putShort(type.toShort())
        bb.putShort(data.size.toShort())
        bb.put(data)
        return bb.array()
    }

    private fun sniExt(sni: ByteArray): ByteArray {
        val entry = byteArrayOf(0x00) + u16Prefix(sni)
        return ext(0x0000, u16Prefix(entry))
    }

    private fun alpnExt(): ByteArray {
        val protos = byteArrayOf(0x02, 'h'.code.toByte(), '2'.code.toByte(),
            0x08, 'h'.code.toByte(), 't'.code.toByte(), 't'.code.toByte(), 'p'.code.toByte(),
            '/'.code.toByte(), '1'.code.toByte(), '.'.code.toByte(), '1'.code.toByte())
        return ext(0x0010, u16Prefix(protos))
    }

    private fun keyShareExt(greaseVal: Int?): ByteArray {
        val ephPub = ByteArray(32).also { rng.nextBytes(it) }
        val x25519 = shorts(0x001D) + u16Prefix(ephPub)
        val ks = if (greaseVal != null) shorts(greaseVal) + byteArrayOf(0x00, 0x01, 0x00) + x25519 else x25519
        return ext(0x0033, u16Prefix(ks))
    }

    private fun shorts(vararg values: Int): ByteArray {
        val bb = ByteBuffer.allocate(values.size * 2)
        values.forEach { bb.putShort(it.toShort()) }
        return bb.array()
    }

    private fun u16Prefix(data: ByteArray): ByteArray {
        val bb = ByteBuffer.allocate(2 + data.size)
        bb.putShort(data.size.toShort())
        bb.put(data)
        return bb.array()
    }

    private fun concat(vararg arrays: ByteArray): ByteArray {
        val total = arrays.sumOf { it.size }
        val result = ByteArray(total)
        var off = 0
        arrays.forEach { it.copyInto(result, off); off += it.size }
        return result
    }

    private operator fun ByteArray.plus(other: ByteArray): ByteArray {
        val r = ByteArray(size + other.size)
        copyInto(r); other.copyInto(r, size); return r
    }
}
