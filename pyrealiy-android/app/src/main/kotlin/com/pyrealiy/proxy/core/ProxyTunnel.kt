package com.pyrealiy.proxy.core

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import java.io.InputStream
import java.io.OutputStream
import java.net.InetSocketAddress
import java.net.Socket
import java.net.SocketTimeoutException
import java.nio.ByteBuffer

/**
 * PyReality 加密隧道客户端
 *
 * 握手流程（同服务端 camouflage.py + tunnel.py）：
 *   1. TCP connect → server
 *   2. Send TLS ClientHello（含 32 字节 session token 作为 session_id）
 *   3. Drain server TLS handshake records（ServerHello + CCS + 加密扩展）
 *   4. Send fake client tail（CCS + fake Finished）
 *   5. 用 client_random 派生 ChaCha20-Poly1305 密钥对
 *   6. 发送目标地址 pack_address(host, port)
 *   7. 双向加密数据中继
 *
 * MAX_RECORD = 16384（TLS 单条记录明文上限）
 */
class ProxyTunnel(
    private val serverHost: String,
    private val serverPort: Int,
    private val password: String,
    private val sni: String,             // 伪装 SNI，如 "www.apple.com"
    // VpnService.protect(socket)：保证此连接绕过自身 TUN，否则会环路
    private val protect: (Socket) -> Unit = {},
) {
    private lateinit var socket: Socket
    private lateinit var sin: InputStream
    private lateinit var sout: OutputStream
    private lateinit var sendCipher: ChaCha20Poly1305Cipher
    private lateinit var recvCipher: ChaCha20Poly1305Cipher
    private var sendNonce = 0L
    private var recvNonce = 0L
    // 并发保护：多个上层 coroutine 可能并发调用 send；不加锁会导致 nonce 重用
    // （ChaCha20-Poly1305 在 nonce 重用下机密性完全失效）以及 TLS 帧字节交错。
    private val sendMutex = Mutex()

    /** 建立连接并完成握手，之后可调用 send/recv */
    suspend fun connect(targetHost: String, targetPort: Int) = withContext(Dispatchers.IO) {
        socket = Socket()
        try {
            // 必须先 bind 创建 fd，再 protect，再 connect；否则 SYN 走 TUN 形成环路
            socket.bind(null)
            protect(socket)
            socket.connect(InetSocketAddress(serverHost, serverPort), 8000)
        } catch (e: Exception) {
            Logger.w(TAG, "TCP connect failed → $targetHost:$targetPort: ${e.message}")
            throw e
        }
        sin  = socket.getInputStream()
        sout = socket.getOutputStream()

        // Step 1+2: 生成 session token，发送 ClientHello（含 token 作为 session_id）
        val sessionToken = makeSessionToken(password)
        val (helloRecord, clientRandom) = ClientHello.build(sni, sessionToken)
        sout.write(helloRecord)
        sout.flush()

        // Step 3: 丢弃服务端 server flight
        try {
            drainServerHandshake()
        } catch (e: Exception) {
            Logger.w(TAG, "handshake drain failed → $targetHost:$targetPort: ${e.message}")
            throw e
        }

        // Step 4: 发送 fake client tail (CCS + fake Finished)
        sout.write(ClientHello.buildFakeClientTail())
        sout.flush()

        // Step 5: 派生会话密钥
        val (sendKey, recvKey) = deriveSessionKeys(password, clientRandom)
        sendCipher = ChaCha20Poly1305Cipher(sendKey)
        recvCipher = ChaCha20Poly1305Cipher(recvKey)

        // Step 6: 发送目标地址（加密）
        send(packAddress(targetHost, targetPort))
    }

    /**
     * 发送明文数据（自动加密分帧，**完整 TLS 1.3 inner content type 模拟**）。
     *
     * 线路格式：
     *   outer: 0x17 0x03 0x03 [2B len] [ciphertext]
     *   plaintext_for_AEAD = data || 0x17   ← inner type = application_data
     *
     * 线程安全：同一 tunnel 多 coroutine 串行化。
     */
    suspend fun send(plaintext: ByteArray) = withContext(Dispatchers.IO) {
        sendMutex.withLock {
            var off = 0
            while (off < plaintext.size) {
                val chunk = plaintext.copyOfRange(off, minOf(off + MAX_RECORD, plaintext.size))
                off += chunk.size
                val nonce = nonceBytes(sendNonce++)
                // 末尾追加 inner content type 0x17 (application_data) —— RFC 8446 §5.2
                val inner = ByteArray(chunk.size + 1)
                chunk.copyInto(inner)
                inner[chunk.size] = 0x17.toByte()
                val ct = sendCipher.encrypt(nonce, inner)
                val frame = ByteBuffer.allocate(5 + ct.size)
                frame.put(0x17.toByte()); frame.put(0x03); frame.put(0x03)
                frame.putShort(ct.size.toShort())
                frame.put(ct)
                sout.write(frame.array())
            }
            sout.flush()
        }
    }

    /**
     * 接收并解密一个 TLS 1.3 风格记录。
     *
     * 外层一律是 0x17（application_data），真实 content type 在 plaintext 末尾。
     * 收到 alert (0x15) 时按对端关闭处理（抛 EOFException 让上层 break 中继）。
     */
    suspend fun recv(): ByteArray = withContext(Dispatchers.IO) {
        val header = readExactly(5)
        val ctLen = ((header[3].toInt() and 0xFF) shl 8) or (header[4].toInt() and 0xFF)
        val ciphertext = readExactly(ctLen)
        val nonce = nonceBytes(recvNonce++)
        val plaintext = recvCipher.decrypt(nonce, ciphertext)
        if (plaintext.isEmpty()) {
            throw java.io.EOFException("empty plaintext (missing inner content type)")
        }
        val innerType = plaintext.last().toInt() and 0xFF
        when (innerType) {
            0x17 -> plaintext.copyOf(plaintext.size - 1)            // application_data
            0x15 -> throw java.io.EOFException("peer sent TLS alert (close_notify)")
            else -> throw java.io.IOException("unknown TLS inner content type 0x${innerType.toString(16)}")
        }
    }

    /**
     * 发一帧加密的 TLS 1.3 close_notify alert，与真实 TLS 1.3 关闭逐字节一致。
     *
     *   outer: 0x17 0x03 0x03 0x00 0x13 [19B ciphertext]
     *   plaintext_for_AEAD = 0x01 0x00 0x15
     */
    suspend fun sendCloseNotify() = withContext(Dispatchers.IO) {
        try {
            sendMutex.withLock {
                val nonce = nonceBytes(sendNonce++)
                val inner = byteArrayOf(0x01, 0x00, 0x15.toByte())
                val ct = sendCipher.encrypt(nonce, inner)
                val frame = ByteBuffer.allocate(5 + ct.size)
                frame.put(0x17.toByte()); frame.put(0x03); frame.put(0x03)
                frame.putShort(ct.size.toShort())
                frame.put(ct)
                sout.write(frame.array())
                sout.flush()
            }
        } catch (_: Exception) { /* 对端已关或 writer 已断，静默 */ }
    }

    fun close() = runCatching { socket.close() }

    // ── TLS 服务端握手 drain ─────────────────────────────────────────────────

    /**
     * 丢弃服务端整个 TLS 1.3 server flight：
     *   ServerHello(0x16) + CCS(0x14) + 1~6 个加密记录(0x17)
     *
     * 服务端 handshake_cache._fetch_one 硬限制 enc_count <= 6，
     * 所以收满 6 条加密记录即可立即退出，无需等超时（最快路径）。
     * 否则在 CCS 之后用 500ms 短超时兜底（兼顾移动网络重传抖动，比 2s 省 1.5s）。
     */
    private fun drainServerHandshake() {
        var sawCcs = false
        var encCount = 0
        while (true) {
            socket.soTimeout = if (sawCcs) 500 else 10_000
            val hdr = try {
                readExactly(5)
            } catch (_: SocketTimeoutException) {
                if (sawCcs) break  // flight ended
                throw java.io.IOException("Timeout waiting for server handshake")
            }
            val len = ((hdr[3].toInt() and 0xFF) shl 8) or (hdr[4].toInt() and 0xFF)
            socket.soTimeout = 5_000  // body should follow header immediately
            readExactly(len)          // discard body
            val ct = hdr[0].toInt() and 0xFF
            if (ct == 0x14) sawCcs = true
            else if (sawCcs && ct == 0x17) {
                encCount++
                if (encCount >= 6) break  // 达到服务端硬上限，零等待退出
            }
        }
        socket.soTimeout = 0  // restore blocking reads for recv()
    }

    private fun readExactly(n: Int): ByteArray {
        val buf = ByteArray(n)
        var off = 0
        while (off < n) {
            val read = sin.read(buf, off, n - off)
            if (read < 0) throw java.io.EOFException("connection closed")
            off += read
        }
        return buf
    }

    private fun nonceBytes(counter: Long): ByteArray =
        ByteBuffer.allocate(12).apply { putLong(4, counter) }.array()

    companion object {
        private const val TAG = "ProxyTunnel"
        private const val MAX_RECORD = 16384

        /** 同 utils.py pack_address：[1-byte host_len][host][2-byte port BE] */
        fun packAddress(host: String, port: Int): ByteArray {
            val hb = host.toByteArray(Charsets.US_ASCII)
            return byteArrayOf(hb.size.toByte()) + hb +
                    byteArrayOf((port ushr 8).toByte(), port.toByte())
        }

        private operator fun ByteArray.plus(other: ByteArray): ByteArray {
            val r = ByteArray(size + other.size)
            copyInto(r); other.copyInto(r, size); return r
        }
    }
}
