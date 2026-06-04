package com.pyrealiy.proxy.core

import java.nio.ByteBuffer
import java.security.MessageDigest
import java.security.SecureRandom
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec
import javax.crypto.Cipher
import javax.crypto.spec.IvParameterSpec

// ── Token 生成（同 hello_auth.py make_session_token）─────────────────────────

private val rng = SecureRandom()

fun makeSessionToken(password: String): ByteArray {
    val randomPrefix = ByteArray(8).also { rng.nextBytes(it) }
    val ts = System.currentTimeMillis() / 1000L
    val tsBytes = ByteBuffer.allocate(8).putLong(ts).array()

    // mask = HMAC-SHA256(SHA256(password), randomPrefix)[:8]
    val pwKey = sha256(password.toByteArray())
    val mask = hmacSha256(pwKey, randomPrefix).copyOf(8)
    val hiddenTs = ByteArray(8) { i -> (tsBytes[i].toInt() xor mask[i].toInt()).toByte() }

    // Poly1305(key=SHA256(password || tsBytes || randomPrefix)).update(tsBytes)
    // randomPrefix 混进 key（不是 message）：同秒 token 也产生不同的 tag，
    // 消除"同秒 ClientHello 末 16 字节完全一致"的可统计指纹。
    // 注意：random_prefix 不能混进 message，否则 Poly1305 同 key 签不同 message
    // 会导致密钥恢复攻击。
    val oneTimeKey = sha256(password.toByteArray() + tsBytes + randomPrefix)
    val tag = poly1305Tag(oneTimeKey, tsBytes)    // 16 bytes

    return randomPrefix + hiddenTs + tag
}

// ── 会话密钥派生（同 tunnel.py _derive_master / _expand）────────────────────

fun deriveSessionKeys(password: String, clientRandom: ByteArray): Pair<ByteArray, ByteArray> {
    val rawKey = sha256(password.toByteArray())
    val master = hkdfSha256(rawKey, clientRandom, "pyrealiy-session".toByteArray(), 32)
    val sendKey = hkdfExpand(master, "c2s".toByteArray(), 32)
    val recvKey = hkdfExpand(master, "s2c".toByteArray(), 32)
    return Pair(sendKey, recvKey)
}

// ── ChaCha20-Poly1305 封装 ────────────────────────────────────────────────────

class ChaCha20Poly1305Cipher(private val key: ByteArray) {

    fun encrypt(nonce: ByteArray, plaintext: ByteArray): ByteArray {
        val cipher = Cipher.getInstance("ChaCha20-Poly1305")
        cipher.init(Cipher.ENCRYPT_MODE, SecretKeySpec(key, "ChaCha20"), IvParameterSpec(nonce))
        return cipher.doFinal(plaintext)  // ciphertext + 16-byte tag appended by JCA
    }

    fun decrypt(nonce: ByteArray, ciphertextWithTag: ByteArray): ByteArray {
        val cipher = Cipher.getInstance("ChaCha20-Poly1305")
        cipher.init(Cipher.DECRYPT_MODE, SecretKeySpec(key, "ChaCha20"), IvParameterSpec(nonce))
        return cipher.doFinal(ciphertextWithTag)
    }
}

// ── 原语 ──────────────────────────────────────────────────────────────────────

fun sha256(data: ByteArray): ByteArray =
    MessageDigest.getInstance("SHA-256").digest(data)

fun hmacSha256(key: ByteArray, data: ByteArray): ByteArray {
    val mac = Mac.getInstance("HmacSHA256")
    mac.init(SecretKeySpec(key, "HmacSHA256"))
    return mac.doFinal(data)
}

// HKDF-Extract: PRK = HMAC-SHA256(salt, ikm)
// HKDF-Expand:  T(1) = HMAC-SHA256(PRK, info || 0x01), etc.
fun hkdfSha256(ikm: ByteArray, salt: ByteArray, info: ByteArray, length: Int): ByteArray {
    val prk = hmacSha256(salt, ikm)
    return hkdfExpand(prk, info, length)
}

fun hkdfExpand(prk: ByteArray, info: ByteArray, length: Int): ByteArray {
    val mac = Mac.getInstance("HmacSHA256")
    mac.init(SecretKeySpec(prk, "HmacSHA256"))
    val out = ByteArrayBuilder()
    var prev = ByteArray(0)
    var counter = 1
    while (out.size < length) {
        mac.reset()
        mac.update(prev)
        mac.update(info)
        mac.update(counter.toByte())
        prev = mac.doFinal()
        out.append(prev)
        counter++
    }
    return out.toByteArray().copyOf(length)
}

// Poly1305 MAC — RFC 8439 software implementation (key=32 bytes, msg=arbitrary)
fun poly1305Tag(key: ByteArray, msg: ByteArray): ByteArray {
    require(key.size == 32)
    val r = clamp(key.copyOf(16).toLittleEndianBigInt())
    var h = java.math.BigInteger.ZERO
    val p = (java.math.BigInteger.ONE shl 130) - 5.toBigInteger()  // RFC 8439 §2.5
    val s = key.copyOfRange(16, 32).toLittleEndianBigInt()
    val blocks = (msg.size + 15) / 16
    for (i in 0 until blocks) {
        val start = i * 16
        val end = minOf(start + 16, msg.size)
        val block = msg.copyOfRange(start, end) + byteArrayOf(0x01) +
                ByteArray(maxOf(0, 16 - (end - start)))
        h = ((h + block.toLittleEndianBigInt()) * r).mod(p)
    }
    h = (h + s).mod(java.math.BigInteger.ONE shl 128)
    val result = ByteArray(16)
    val hBytes = h.toLittleEndianBytes(16)
    hBytes.copyInto(result)
    return result
}

private fun clamp(n: java.math.BigInteger): java.math.BigInteger {
    // clamp r: clear top 4 bits of byte 3, 7, 11, 15; clear bottom 2 bits of byte 0..15
    var r = n
    val mask = java.math.BigInteger("0ffffffc0ffffffc0ffffffc0fffffff", 16)
    return r.and(mask)
}

private fun ByteArray.toLittleEndianBigInt(): java.math.BigInteger {
    val le = this.copyOf() // don't modify original
    le.reverse()
    return java.math.BigInteger(1, le)
}

private fun java.math.BigInteger.toLittleEndianBytes(len: Int): ByteArray {
    val be = this.toByteArray()
    val out = ByteArray(len)
    // BigInteger BE, may have leading zero sign byte
    val src = if (be.first() == 0.toByte()) be.drop(1).toByteArray() else be
    src.reversed().forEachIndexed { i, b -> if (i < len) out[i] = b }
    return out
}

private class ByteArrayBuilder {
    private val buf = mutableListOf<Byte>()
    val size get() = buf.size
    fun append(arr: ByteArray) { arr.forEach { buf.add(it) } }
    fun toByteArray() = buf.toByteArray()
}

private operator fun ByteArray.plus(other: ByteArray): ByteArray {
    val result = ByteArray(size + other.size)
    copyInto(result); other.copyInto(result, size)
    return result
}
