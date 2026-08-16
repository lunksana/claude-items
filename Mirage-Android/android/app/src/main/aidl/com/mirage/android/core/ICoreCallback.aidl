package com.mirage.android.core;

/** 内核状态回调 (UI 订阅, 跨进程)。 */
interface ICoreCallback {
    void onStateChanged(boolean running);
    void onLog(String line);
}
