# ── VpnService 子类：必须保留，否则系统无法绑定 ───────────────────────────────
-keep class com.pyrealiy.proxy.service.ProxyVpnService { *; }

# ── 数据类（JSON 序列化/反序列化用到字段名）────────────────────────────────────
-keepclassmembers class com.pyrealiy.proxy.data.** { *; }

# ── Kotlin 协程 ───────────────────────────────────────────────────────────────
-keepnames class kotlinx.coroutines.internal.MainDispatcherFactory {}
-keepnames class kotlinx.coroutines.CoroutineExceptionHandler {}
-keepclassmembernames class kotlinx.** {
    volatile <fields>;
}

# ── 加密类（内部反射访问 Cipher/Mac）────────────────────────────────────────────
-keep class com.pyrealiy.proxy.core.** { *; }

# ── 调试信息：保留行号（便于崩溃堆栈定位）──────────────────────────────────────
-keepattributes SourceFile,LineNumberTable
-renamesourcefileattribute SourceFile

# ── Material Components（反射构造 View）─────────────────────────────────────────
-keep class com.google.android.material.** { *; }
-dontwarn com.google.android.material.**
