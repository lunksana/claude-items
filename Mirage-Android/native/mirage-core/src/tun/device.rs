//! `TunDevice` —— smoltcp `phy::Device` 与 TUN 接口之间的桥接层。
//!
//! 与上游 WG 隧道的 `WgDevice` 同构 (纯队列搬运), 只是队列两端接的是 **TUN fd**
//! 而非 WireGuard 隧道:
//!
//! ```text
//!  TUN fd ─read─▶ rx 队列 ─▶ smoltcp Interface::poll ─▶ tx 队列 ─write─▶ TUN fd
//! ```
//!
//! smoltcp 的 `Device` 是同步接口 (`receive`/`transmit` 不能 await), 而 TUN fd 的
//! 读写是阻塞 IO, 所以用队列 + 后台线程解耦: 读线程把 fd 上来的 IP 包塞进 rx 队列,
//! poll 循环把 smoltcp 产出的 IP 包从 tx 队列取出写回 fd。
//!
//! 队列有上限: 应用侧背压时无限堆积会 OOM。满了丢最老的包 —— IP 层本就尽力而为,
//! 丢包由上层 TCP 重传兜住 (UDP 则本就允许丢)。

use smoltcp::phy::{self, Device, DeviceCapabilities, Medium};
use smoltcp::time::Instant;
use std::collections::VecDeque;

/// 单向队列的包数上限。1500B MTU 下 512 包 ≈ 768KB, 够吸收突发又不至于 OOM。
const QUEUE_CAP: usize = 512;

pub struct TunDevice {
    rx: VecDeque<Vec<u8>>,
    tx: VecDeque<Vec<u8>>,
    mtu: usize,
}

impl TunDevice {
    pub fn new(mtu: usize) -> Self {
        Self { rx: VecDeque::new(), tx: VecDeque::new(), mtu }
    }

    /// poll 循环: 塞一个从 TUN fd 读到的入站 IP 包给 smoltcp。满则丢最老的。
    pub fn push_rx(&mut self, pkt: Vec<u8>) {
        if self.rx.len() >= QUEUE_CAP {
            self.rx.pop_front();
        }
        self.rx.push_back(pkt);
    }

    /// poll 循环: 取一个 smoltcp 产出的出站 IP 包去写 TUN fd。
    pub fn pop_tx(&mut self) -> Option<Vec<u8>> {
        self.tx.pop_front()
    }

    /// tx 队列是否还有未取走的包 (决定 pump 是否要再写一轮)。
    pub fn has_tx(&self) -> bool {
        !self.tx.is_empty()
    }
}

pub struct TunRxToken(Vec<u8>);
pub struct TunTxToken<'a>(&'a mut VecDeque<Vec<u8>>);

impl phy::RxToken for TunRxToken {
    fn consume<R, F>(self, f: F) -> R
    where
        F: FnOnce(&[u8]) -> R,
    {
        f(&self.0)
    }
}

impl phy::TxToken for TunTxToken<'_> {
    fn consume<R, F>(self, len: usize, f: F) -> R
    where
        F: FnOnce(&mut [u8]) -> R,
    {
        let mut buf = vec![0u8; len];
        let r = f(&mut buf);
        if self.0.len() >= QUEUE_CAP {
            self.0.pop_front();
        }
        self.0.push_back(buf);
        r
    }
}

impl Device for TunDevice {
    type RxToken<'a> = TunRxToken where Self: 'a;
    type TxToken<'a> = TunTxToken<'a> where Self: 'a;

    fn receive(&mut self, _t: Instant) -> Option<(Self::RxToken<'_>, Self::TxToken<'_>)> {
        let pkt = self.rx.pop_front()?;
        Some((TunRxToken(pkt), TunTxToken(&mut self.tx)))
    }

    fn transmit(&mut self, _t: Instant) -> Option<Self::TxToken<'_>> {
        Some(TunTxToken(&mut self.tx))
    }

    fn capabilities(&self) -> DeviceCapabilities {
        let mut c = DeviceCapabilities::default();
        // Medium::Ip = 裸 IP 包, 无以太网头/ARP —— TUN 正是这个模型。
        c.medium = Medium::Ip;
        c.max_transmission_unit = self.mtu;
        c
    }
}
