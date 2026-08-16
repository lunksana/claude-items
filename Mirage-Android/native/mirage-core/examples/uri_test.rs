fn main() {
    let cases = [
        "mirage://0a416d99b7248df5e3242bfd8ce1982e@192.220.100.83:443/?sni=speedtest.net",
        "mirage://pass@1.2.3.4:443?sni=www.apple.com",
        "mirage://pw@[2606:4700:4700::1111]:443?sni=x.com",
    ];
    for u in cases {
        match mirage_core::node_uri::NodeUri::parse(u) {
            Ok(n) => println!("OK  host={} port={} sni={}", n.host, n.port, n.sni),
            Err(e) => println!("ERR {e}"),
        }
    }
}
