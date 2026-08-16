use std::io::Write;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::collections::VecDeque;

pub static GLOBAL_UP: AtomicU64 = AtomicU64::new(0);
pub static GLOBAL_DOWN: AtomicU64 = AtomicU64::new(0);
pub static DROPPED_LOGS: AtomicU64 = AtomicU64::new(0);

pub fn add_up(bytes: u64) {
    GLOBAL_UP.fetch_add(bytes, Ordering::Relaxed);
}

pub fn add_down(bytes: u64) {
    GLOBAL_DOWN.fetch_add(bytes, Ordering::Relaxed);
}

// ── 流量采样 (实时速率) ────────────────────────────────────────────────────

/// 最近样本 (时间, 累计上行, 累计下行)。保留 20 个, 间隔 ≥800ms 才记 (过滤 App 高频轮询噪声)。
static RATE_SAMPLES: std::sync::Mutex<VecDeque<(std::time::Instant, u64, u64)>> =
    std::sync::Mutex::new(VecDeque::new());

/// 采样并返回 (up_total, down_total, up_rate_bps, down_rate_bps)。
/// 速率 = 最近两个样本的字节差 / 时间差。
pub fn sample() -> (u64, u64, f64, f64) {
    let now = std::time::Instant::now();
    let (up, down) = (GLOBAL_UP.load(Ordering::Relaxed), GLOBAL_DOWN.load(Ordering::Relaxed));
    let mut q = RATE_SAMPLES.lock().unwrap_or_else(|e| e.into_inner());
    let (mut up_rate, mut down_rate) = (0f64, 0f64);
    if let Some((prev_t, prev_up, prev_dn)) = q.back() {
        let dt = now.duration_since(*prev_t).as_secs_f64();
        if dt >= 0.8 && dt > 0.0 {
            up_rate = (up - prev_up) as f64 / dt;
            down_rate = (down - prev_dn) as f64 / dt;
            q.push_back((now, up, down));
            while q.len() > 20 {
                q.pop_front();
            }
        }
    } else {
        q.push_back((now, up, down));
    }
    (up, down, up_rate, down_rate)
}

#[derive(Clone)]
pub struct MemoryWriter {
    tx: std::sync::mpsc::SyncSender<String>,
    buffer: Arc<Mutex<VecDeque<String>>>,
}

impl Default for MemoryWriter {
    fn default() -> Self {
        Self::new()
    }
}

impl MemoryWriter {
    pub fn new() -> Self {
        let (tx, rx) = std::sync::mpsc::sync_channel(1000);
        let buffer = Arc::new(Mutex::new(VecDeque::with_capacity(500)));
        let bg_buf = buffer.clone();
        
        std::thread::spawn(move || {
            while let Ok(s) = rx.recv() {
                let mut q = bg_buf.lock().unwrap_or_else(|e| e.into_inner());
                if q.len() >= 500 {
                    q.pop_front();
                }
                q.push_back(s);
            }
        });
        
        Self {
            tx,
            buffer,
        }
    }

    /// 追加一行到环形日志。
    pub fn write_line(&self, line: &str) {
        let mut q = self.buffer.lock().unwrap_or_else(|e| e.into_inner());
        if q.len() >= 500 {
            q.pop_front();
        }
        q.push_back(line.to_string());
    }

    pub fn get_logs(&self) -> Vec<String> {
        let q = self.buffer.lock().unwrap_or_else(|e| e.into_inner());
        q.iter().cloned().collect()
    }
}

impl Write for MemoryWriter {
    fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
        let s = String::from_utf8(buf.to_vec())
            .unwrap_or_else(|e| String::from_utf8_lossy(e.as_bytes()).into_owned());
        if s.trim().is_empty() {
            return Ok(buf.len());
        }
        
        if self.tx.try_send(s).is_err() {
            DROPPED_LOGS.fetch_add(1, Ordering::Relaxed);
        }
        Ok(buf.len())
    }

    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

/// 全局内存日志 (OnceLock, 替代上游 lazy_static)。
pub fn global_logger() -> &'static MemoryWriter {
    use std::sync::OnceLock;
    static LOGGER: OnceLock<MemoryWriter> = OnceLock::new();
    LOGGER.get_or_init(MemoryWriter::new)
}

/// 最近 N 条日志 (App 日志面板用)。
pub fn recent_logs() -> Vec<String> {
    global_logger().get_logs()
}

/// 追加一行日志 (tracing writer 转发用)。
pub fn recent_logs_push(line: &str) {
    let mut s = line.to_string();
    if s.ends_with('\n') {
        s.pop();
    }
    global_logger().write_line(&s);
}
