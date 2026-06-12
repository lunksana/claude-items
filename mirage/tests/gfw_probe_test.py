import asyncio
import os
import struct
import time
import json
import sys

# Ensure core modules can be imported
sys.path.append("/opt/claude/mirage-proxy")

try:
    from core.hello_auth import make_session_token
    from core.tls_raw import build_client_hello
except ImportError as e:
    print(f"Error importing core modules: {e}")
    sys.exit(1)

SERVER_HOST = "127.0.0.1"
SERVER_PORT = 443
PASSWORD = "your_secure_password" # Will be overwritten by config_server.json
CAMOUFLAGE_HOST = "www.apple.com"

# TLS 1.3 ServerHello Constants
TLS_HANDSHAKE_TYPE = 0x16
SERVER_HELLO_TYPE = 0x02

async def send_and_receive(payload: bytes, timeout: float = 3.0) -> bytes:
    """Send a payload and read all available response bytes."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(SERVER_HOST, SERVER_PORT), timeout=3.0
        )
    except Exception as e:
        print(f"[-] Connection failed: {e}")
        return b""

    writer.write(payload)
    await writer.drain()

    resp = bytearray()
    try:
        while True:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=timeout)
            if not chunk:
                break
            resp.extend(chunk)
    except asyncio.TimeoutError:
        pass # Expected for some probes
    except Exception as e:
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

    return bytes(resp)

def parse_server_hello(resp: bytes):
    """Parse ServerHello to extract server_random and session_id_echo."""
    if len(resp) < 44 or resp[0] != TLS_HANDSHAKE_TYPE or resp[5] != SERVER_HELLO_TYPE:
        return None, None
    server_random = resp[11:43]
    sid_len = resp[43]
    if len(resp) < 44 + sid_len:
        return None, None
    session_id_echo = resp[44:44+sid_len]
    return server_random, session_id_echo

async def test_garbage_probe():
    print("\n[+] Test 1: Garbage Probing (Random bytes)")
    garbage = os.urandom(256)
    resp = await send_and_receive(garbage, timeout=2.0)
    
    if len(resp) == 0:
        print("    [PASS] Server silently dropped the garbage connection (or closed cleanly).")
    else:
        # GFW scanners often send random data and check if the server crashes or leaks proxy specific info
        print(f"    [INFO] Server returned {len(resp)} bytes. First bytes: {resp[:10].hex()}")
        if resp[0] == 0x15: # TLS Alert
            print("    [PASS] Server returned a standard TLS Alert.")
        else:
            print("    [WARN] Server returned non-standard data. Check if this is a fingerprint.")

async def test_tls_active_probe():
    print("\n[+] Test 2: Active TLS Probing (Simulating OpenSSL s_client)")
    # Generate a random session ID (acting as a normal scanner, NOT our proxy auth token)
    probe_session_id = os.urandom(32)
    fake_hello, _ = build_client_hello(CAMOUFLAGE_HOST, probe_session_id)
    
    print("    -> Sending Probe 1...")
    resp1 = await send_and_receive(fake_hello, timeout=2.0)
    rand1, sid_echo1 = parse_server_hello(resp1)
    
    print("    -> Sending Probe 2...")
    resp2 = await send_and_receive(fake_hello, timeout=2.0)
    rand2, sid_echo2 = parse_server_hello(resp2)

    if not rand1 or not rand2:
        print("    [FAIL] Did not receive a valid ServerHello. Is handshake_cache populated?")
        return

    # Check 1: Session ID Echo behavior (Fix for A2 defect)
    if sid_echo1 == probe_session_id and sid_echo2 == probe_session_id:
        print("    [PASS] session_id_echo exactly matches our probe's ClientHello (Perfect TLS 1.3 mimicry).")
    else:
        print(f"    [FAIL] session_id_echo mismatch! Expected: {probe_session_id.hex()[:10]}... Got: {sid_echo1.hex()[:10]}...")

    # Check 2: Server Random uniqueness (Fix for A2 defect)
    if rand1 != rand2:
        print("    [PASS] server_random is unique across multiple connections (Resistant to statistical clustering).")
    else:
        print("    [FAIL] server_random is IDENTICAL! GFW can cluster this as a proxy fingerprint.")

async def test_replay_attack():
    print("\n[+] Test 3: Replay Attack (Simulating GFW capturing and replaying a valid proxy session)")
    
    # Construct a VALID Mirage auth payload
    token = make_session_token(PASSWORD)
    valid_hello, _ = build_client_hello(CAMOUFLAGE_HOST, token)
    
    print("    -> Sending Original Valid Session...")
    try:
        r1, w1 = await asyncio.wait_for(asyncio.open_connection(SERVER_HOST, SERVER_PORT), timeout=3.0)
        w1.write(valid_hello)
        await w1.drain()
        resp1 = await asyncio.wait_for(r1.read(4096), timeout=2.0)
        
        if len(resp1) > 0 and resp1[0] == TLS_HANDSHAKE_TYPE:
            print(f"    [INFO] Original session accepted. Server replied with {len(resp1)} bytes.")
        else:
            print("    [WARN] Original session failed or was dropped. Check password/server status.")
            return
            
        w1.close()
    except Exception as e:
        print(f"    [FAIL] Could not establish original connection: {e}")
        return

    print("    -> Waiting 0.5 seconds...")
    await asyncio.sleep(0.5)
    
    print("    -> Replaying the EXACT same payload (Same token, same timestamps)...")
    try:
        r2, w2 = await asyncio.wait_for(asyncio.open_connection(SERVER_HOST, SERVER_PORT), timeout=3.0)
        w2.write(valid_hello)
        await w2.drain()
        
        # A real proxy connection won't return anything until we send a target address
        # A camouflage connection WILL return the fake ServerHello (which starts with 0x16)
        # So if we receive bytes here starting with 0x16, we know it correctly treated it as a probe
        try:
            resp2 = await asyncio.wait_for(r2.read(4096), timeout=2.0)
            if len(resp2) > 0 and resp2[0] == TLS_HANDSHAKE_TYPE:
                print("    [PASS] Replayed session was treated as a probe (camouflage).")
            else:
                print(f"    [FAIL] Replayed session returned unexpected data: {resp2.hex()[:20]}")
        except asyncio.TimeoutError:
            print("    [FAIL] Replayed session timed out! It was accepted as a proxy connection (bypassed ReplayCache).")
        w2.close()
            
    except Exception as e:
        print(f"    [INFO] Error during strict replay test (expected if silently dropped): {e}")

async def main():
    global PASSWORD
    print(f"Starting GFW Probe Simulation against {SERVER_HOST}:{SERVER_PORT}")
    print("Note: Make sure your Mirage server is RUNNING locally for this test.")
    
    # Load config to get the correct password
    try:
        with open("/opt/claude/mirage-proxy/config_server.json", "r") as f:
            cfg = json.load(f)
            PASSWORD = cfg.get("password", PASSWORD)
            print(f"[INFO] Loaded password from config_server.json")
    except FileNotFoundError:
        print("[WARN] config_server.json not found. Using default password 'your_secure_password'.")

    await test_garbage_probe()
    await test_tls_active_probe()
    await test_replay_attack()
    print("\n[=] Security Tests Completed.")

if __name__ == "__main__":
    asyncio.run(main())
