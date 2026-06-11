import asyncio
import time
import struct

PROXY_LISTEN_PORT = 8443  # Our middlebox listens here
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 443         # The real PyRealiy server

# Simulated network conditions (Cross-ocean)
LATENCY_SEC = 0.150       # 150ms latency
PACKET_LOSS_RATE = 0.00   # 0% packet loss (can increase to test Brutal retransmission)

class TrafficAnalyzer:
    def __init__(self):
        self.conn_times = []
        self.record_sizes = []
        self.lock = asyncio.Lock()

    async def log_connection(self):
        now = time.monotonic()
        async with self.lock:
            if self.conn_times:
                gap = now - self.conn_times[-1]
                print(f"[Traffic] 🆕 New Connection (SYN). Gap from previous: {gap:.3f}s")
            else:
                print(f"[Traffic] 🆕 First Connection (SYN).")
            self.conn_times.append(now)

    def analyze_tls_records(self, data: bytes):
        """
        Scan raw bytes for TLS 1.3 Application Data records (0x17)
        and record their ciphertext lengths to verify bucketing.
        """
        idx = 0
        while idx <= len(data) - 5:
            # Look for Application Data (0x17) + Protocol Version (0x0303)
            if data[idx] == 0x17 and data[idx+1] == 0x03 and data[idx+2] == 0x03:
                record_len = struct.unpack("!H", data[idx+3:idx+5])[0]
                if record_len > 0 and (idx + 5 + record_len) <= len(data):
                    self.record_sizes.append(record_len)
                    # Deduce bucket size (subtracting 16 bytes auth tag + 1 byte inner type)
                    actual_payload = record_len - 17
                    print(f"  [TLS Record] Captured length: {record_len} bytes (approx chunk size: {actual_payload})")
                    idx += 5 + record_len
                    continue
            idx += 1

analyzer = TrafficAnalyzer()

async def forward_stream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, direction: str):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            
            # Simulate Passive Traffic Analysis (GFW observing sizes)
            if direction == "C->S":
                analyzer.analyze_tls_records(data)
                
            # Simulate Latency
            if LATENCY_SEC > 0:
                await asyncio.sleep(LATENCY_SEC)
                
            writer.write(data)
            await writer.drain()
    except Exception as e:
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

async def handle_middlebox(client_reader, client_writer):
    await analyzer.log_connection()
    try:
        server_reader, server_writer = await asyncio.open_connection(SERVER_HOST, SERVER_PORT)
    except Exception as e:
        print(f"[!] Server not running on {SERVER_PORT}. {e}")
        client_writer.close()
        return

    task1 = asyncio.create_task(forward_stream(client_reader, server_writer, "C->S"))
    task2 = asyncio.create_task(forward_stream(server_reader, client_writer, "S->C"))
    await asyncio.gather(task1, task2)

async def main():
    server = await asyncio.start_server(handle_middlebox, '127.0.0.1', PROXY_LISTEN_PORT)
    print(f"🚀 [Middlebox] Listening on 127.0.0.1:{PROXY_LISTEN_PORT}")
    print(f"⏳ [Middlebox] Simulated Latency: {LATENCY_SEC*1000} ms")
    print(f"🔍 [Middlebox] Analyzing TLS Record bucketing and SYN staggering...")
    print(f"👉 INSTRUCTION: Point your PyRealiy CLIENT's 'server_port' to {PROXY_LISTEN_PORT} instead of {SERVER_PORT}")
    print(f"👉 Then make multiple SOCKS5 connections to observe the staggering in real time.\n")
    
    async with server:
        await server.serve_forever()

if __name__ == "__main__":
    asyncio.run(main())
