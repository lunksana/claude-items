fn main() {
    use mirage_core::direct;
    let cases = [
        ("114.114.114.114", "百度 DNS → 直连"),
        ("8.8.8.8", "谷歌 DNS → 代理"),
        ("223.5.5.5", "阿里 DNS → 直连"),
    ];
    for (ip, desc) in cases {
        let r = direct::should_direct(None, Some(ip.parse().unwrap()));
        println!("{ip} ({desc}) → {}", if r { "直连" } else { "代理" });
    }
    for d in ["www.baidu.com", "www.google.com", "mp.weixin.qq.com"] {
        println!("{d} → {}", if direct::is_cn_domain(d) { "直连" } else { "代理" });
    }
}
