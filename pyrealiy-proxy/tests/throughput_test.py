import asyncio
import time
import os
import struct
import socket

SOCKS_PORT = 1080
TARGET_PORT = 9999
TEST_DURATION = 10.0  # seconds

async def sink_server(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
    except Exception:
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

async def tcp_throughput_test():
    # Start a mock target server that discards all received data
    server = await asyncio.start_server(sink_server, '127.0.0.1', TARGET_PORT)
    print(f"[TCP Test] Local target sink started on port {TARGET_PORT}")
    
    try:
        # Connect to local PyRealiy SOCKS5 client
        print(f"[TCP Test] Connecting to PyRealiy client (SOCKS5) on port {SOCKS_PORT}...")
        try:
            reader, writer = await asyncio.open_connection('127.0.0.1', SOCKS_PORT)
        except Exception as e:
            print(f"[-] Could not connect to SOCKS5 port: {e}")
            return
        
        # SOCKS5 Handshake (No Auth)
        writer.write(b'\x05\x01\x00')
        await writer.drain()
        auth_resp = await reader.readexactly(2)
        if auth_resp != b'\x05\x00':
            print("[-] SOCKS5 auth failed or requires password.")
            return
            
        # SOCKS5 Connect to Target
        req = b'\x05\x01\x00\x01' + socket.inet_aton('127.0.0.1') + struct.pack('!H', TARGET_PORT)
        writer.write(req)
        await writer.drain()
        resp = await reader.readexactly(10)
        if resp[1] != 0x00:
            print(f"[-] SOCKS5 connect request failed with status: {resp[1]}")
            return
        
        print("[TCP Test] Proxy tunnel established! Blasting data for 10 seconds...")
        chunk = os.urandom(65536)
        start_time = time.time()
        last_print = start_time
        bytes_sent = 0
        
        # Measure throughput
        while time.time() - start_time < TEST_DURATION:
            writer.write(chunk)
            await writer.drain()
            bytes_sent += len(chunk)
            
            now = time.time()
            if now - last_print > 1.0:
                mbps = (bytes_sent * 8) / (1024 * 1024 * (now - start_time))
                print(f"  -> Sending... Current speed: {mbps:.2f} Mbps")
                last_print = now
                
        try:
            writer.write_eof()
            # await writer.wait_closed() # Don't wait, it might hang
        except Exception:
            pass
        
        elapsed = time.time() - start_time
        mbps = (bytes_sent * 8) / (1024 * 1024 * elapsed)
        
        print("\n" + "="*40)
        print("📊 THROUGHPUT TEST RESULTS (TCP)")
        print("="*40)
        print(f"Time Elapsed : {elapsed:.2f} seconds")
        print(f"Data Sent    : {bytes_sent / (1024*1024):.2f} MB")
        print(f"Speed        : {mbps:.2f} Mbps")
        print("="*40 + "\n")
        
    finally:
        server.close()
        await server.wait_closed()

if __name__ == "__main__":
    asyncio.run(tcp_throughput_test())
