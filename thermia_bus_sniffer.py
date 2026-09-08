#!/usr/bin/env python3
"""
thermia_bus_sniffer.py - passive sniffer for the internal RS485 / Modbus RTU bus of Thermia / Danfoss heat pumps
(iTec, iTec Eco, Atec, DHP-AQ family; likely Diplomat / Legend too).

PASSIVE ONLY: this tool never transmits on the bus. It listens, validates Modbus RTU frames (CRC16),
prints a live inventory of who talks to whom, decodes the accessory-slot polls (FC23 / 0x17) and writes
a shareable capture log. Safe to run on a working heat pump.

Hardware: any USB-RS485 adapter (or an RS485<->TCP bridge such as Elfin EE11A) on the RJ45 "Modbus" port
of the indoor unit (ports 122/123). Pinout seen so far: pins 1+2 = A, 3+4 = B, 5+6 = GND, 7+8 = +12 V.
Bus settings: 9600 baud, 8 data bits, EVEN parity, 1 stop bit.

Usage:
    python thermia_bus_sniffer.py --com COM6                      # Windows serial
    python thermia_bus_sniffer.py --com /dev/ttyUSB0 --seconds 120
    python thermia_bus_sniffer.py --tcp 192.0.2.182:8899          # RS485<->TCP bridge
    python thermia_bus_sniffer.py --com COM6 --focus 0x06         # decode only the online-module slot
    python thermia_bus_sniffer.py --replay capture.log            # re-decode a saved log offline

Output:
    <name>.log   one frame per line:  <seconds since start>  <hex>      -> this is the file to share
    console      inventory every --every seconds + decoded slot traffic

Requires: pyserial (pip install pyserial) for --com; nothing else.
License: MIT.
"""
import sys, time, argparse, socket
from collections import defaultdict

# ----------------------------------------------------------------------------- Modbus helpers
def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc

def crc_ok(fr: bytes) -> bool:
    return len(fr) >= 4 and crc16(fr[:-2]) == (fr[-2] | (fr[-1] << 8))

def u16(b, i): return (b[i] << 8) | b[i + 1]
def s16(v): return v - 65536 if v >= 32768 else v

def candidate_lengths(buf, i):
    """Possible frame lengths starting at buf[i], derived from the function code (request AND response shapes)."""
    rem = len(buf) - i
    if rem < 4:
        return []
    fc = buf[i + 1]
    out = set()
    if fc in (0x01, 0x02, 0x03, 0x04):
        out.add(8)                                   # request
        out.add(3 + buf[i + 2] + 2)                  # response (byte count)
    if fc in (0x05, 0x06):
        out.add(8)                                   # request == response
    if fc == 0x10:
        if i + 6 < len(buf):
            out.add(7 + buf[i + 6] + 2)              # request (byte count at [6])
        out.add(8)                                   # response
    if fc == 0x17:
        if i + 10 < len(buf):
            out.add(11 + buf[i + 10] + 2)            # request (write byte count at [10])
        out.add(3 + buf[i + 2] + 2)                  # response (read byte count at [2])
    if fc & 0x80:
        out.add(5)                                   # exception
    return sorted(c for c in out if 4 <= c <= rem)

def extract_frames(buf: bytearray):
    """Pull complete, CRC-valid frames from the FRONT of buf; leave an incomplete tail in place."""
    frames, i = [], 0
    while i < len(buf):
        hit = None
        for L in candidate_lengths(buf, i):
            ch = bytes(buf[i:i + L])
            if crc_ok(ch):
                hit = ch
                break
        if hit:
            frames.append(hit)
            i += len(hit)
        else:
            if len(buf) - i > 64:      # garbage: skip a byte (only when we have plenty of data)
                i += 1
            else:
                break                  # maybe an incomplete frame - wait for more bytes
    del buf[:i]
    return frames

def is_fc23_request(fr):
    if len(fr) < 13 or fr[1] != 0x17:
        return None
    b = fr[2:-2]
    wbc = b[8]
    if len(fr) != 2 + 9 + wbc + 2:
        return None
    return {"rstart": u16(b, 0), "rqty": u16(b, 2), "wstart": u16(b, 4), "wqty": u16(b, 6),
            "wdata": [u16(b, 9 + 2 * k) for k in range(wbc // 2)]}

def looks_like_response(fr, pending_match):
    """True if the frame has the shape of a slave RESPONSE. Requests of FC03/04 and responses with a 3-byte
    payload both have length 8 - only then the request/response pairing decides."""
    fc, n = fr[1], len(fr)
    if fc == 0x17:
        return is_fc23_request(fr) is None
    if fc == 0x10:
        return n == 8 and not (n == 7 + fr[6] + 2)
    if fc in (0x03, 0x04):
        if n != 8:
            return True                       # only responses can have other lengths
        return pending_match                  # ambiguous: trust the pairing
    if fc in (0x05, 0x06):
        return pending_match
    return pending_match

def regs_of_response(fr):
    """Registers carried by an FC03/FC04/FC17 response."""
    bc = fr[2]
    body = fr[3:3 + bc]
    return [u16(body, k) for k in range(0, len(body) - 1, 2)]

KNOWN = {0x02: "indoor board (data)", 0x0F: "indoor board (setpoints, write-only)", 0x1E: "outdoor unit",
         0x06: "ONLINE MODULE slot", 0x0A: "ROOM SENSOR slot"}

# ----------------------------------------------------------------------------- sources
class SerialSource:
    def __init__(self, port):
        import serial
        self.ser = serial.Serial(port, 9600, bytesize=8, parity=serial.PARITY_EVEN, stopbits=1, timeout=0.02)
    def read(self): return self.ser.read(512)
    def close(self): self.ser.close()

class TcpSource:
    def __init__(self, hostport):
        host, port = hostport.rsplit(":", 1)
        self.s = socket.create_connection((host, int(port)), timeout=5)
        self.s.settimeout(0.05)
    def read(self):
        try:
            return self.s.recv(4096)
        except socket.timeout:
            return b""
    def close(self): self.s.close()

class ReplaySource:
    """Reads a .log written by this tool (lines: '<t> <hex>') or a bare hex dump."""
    def __init__(self, path):
        self.chunks = []
        for line in open(path, encoding="utf-8", errors="replace"):
            parts = line.split()
            if not parts:
                continue
            hx = parts[-1]
            try:
                self.chunks.append(bytes.fromhex(hx))
            except ValueError:
                pass
        self.i = 0
    def read(self):
        if self.i >= len(self.chunks):
            return None
        c = self.chunks[self.i]; self.i += 1
        return c
    def close(self): pass

# ----------------------------------------------------------------------------- main loop
def run(src, out_path, seconds, every, focus, quiet):
    inv = defaultdict(lambda: {"req": 0, "resp": 0, "exc": 0, "last": ""})
    pending = {}            # slave -> (fc, request frame, t)  waiting for its response
    buf = bytearray()
    t0 = time.time()
    n_frames = 0
    log = open(out_path, "w", encoding="utf-8") if out_path else None
    last_print = t0
    print(f"listening... (log: {out_path or '-'}; focus: {('0x%02X' % focus) if focus else 'all'}; Ctrl+C to stop)")
    try:
        while True:
            chunk = src.read()
            if chunk is None:
                break                              # replay exhausted
            now = time.time()
            if chunk:
                buf += chunk
                for fr in extract_frames(buf):
                    n_frames += 1
                    addr, fc = fr[0], fr[1]
                    t_rel = now - t0
                    if log:
                        log.write(f"{t_rel:9.3f} {fr.hex()}\n")
                    e = inv[addr]; e["last"] = fr.hex()
                    # request vs response - decided by the frame SHAPE first, by pairing only when ambiguous
                    is_resp = looks_like_response(fr, addr in pending and pending[addr][0] == fc)
                    if fc & 0x80:
                        e["exc"] += 1; pending.pop(addr, None); kind = "EXC "
                    elif is_resp:
                        e["resp"] += 1; pending.pop(addr, None); kind = "RESP"
                    else:
                        e["req"] += 1; pending[addr] = (fc, fr, now); kind = "REQ "
                    if focus is not None and addr == focus:
                        describe(kind, fr, t_rel)
                    elif not quiet and fc == 0x17 and addr in (0x06, 0x0A) and focus is None:
                        describe(kind, fr, t_rel)
                # expire stale pendings (>0.5 s without response = the slot is silent)
                for a in [a for a, (_, _, t) in pending.items() if now - t > 0.5]:
                    pending.pop(a)
            if seconds and now - t0 >= seconds:
                break
            if every and now - last_print >= every:
                print_inventory(inv, now - t0, n_frames); last_print = now
    except KeyboardInterrupt:
        pass
    finally:
        src.close()
        if log:
            log.close()
    print_inventory(inv, time.time() - t0, n_frames)
    if out_path:
        print(f"\nCapture saved to: {out_path}  <- share this file (it contains only bus frames, no personal data)")

def describe(kind, fr, t_rel):
    addr, fc = fr[0], fr[1]
    tag = KNOWN.get(addr, "")
    if fc == 0x17 and kind == "REQ ":
        q = is_fc23_request(fr)
        if q:
            print(f"[{t_rel:8.2f}] REQ  -> 0x{addr:02X} {tag}: FC23 read {q['rqty']} regs @{q['rstart']} | "
                  f"write {q['wqty']} regs @{q['wstart']} = {q['wdata']}   ({fr.hex()})")
            return
    if kind == "RESP" and fc in (0x03, 0x04, 0x17):
        print(f"[{t_rel:8.2f}] RESP <- 0x{addr:02X} {tag}: FC{fc} regs = {regs_of_response(fr)}   ({fr.hex()})  <<< SLOT ANSWERED")
        return
    if kind == "EXC ":
        print(f"[{t_rel:8.2f}] EXC  <- 0x{addr:02X} {tag}: function {fc & 0x7F} code {fr[2]}   ({fr.hex()})")
        return
    print(f"[{t_rel:8.2f}] {kind} 0x{addr:02X} FC{fc:02d} {tag}: {fr.hex()}")

def print_inventory(inv, elapsed, n):
    print(f"\n--- inventory after {elapsed:.0f} s, {n} valid frames ---")
    print(f"{'addr':>6} {'req':>5} {'resp':>5} {'exc':>4}  {'role / note':32} last frame")
    for a in sorted(inv):
        e = inv[a]
        note = KNOWN.get(a, "unknown device")
        if a in (0x06, 0x0A):
            note += "  (ANSWERING!)" if e["resp"] else "  (silent = slot empty)"
        print(f"  0x{a:02X} {e['req']:5d} {e['resp']:5d} {e['exc']:4d}  {note:32} {e['last'][:40]}")
    print("---")

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Passive sniffer for the Thermia/Danfoss heat-pump RS485 Modbus bus (never transmits).")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--com", help="serial port of the USB-RS485 adapter, e.g. COM6 or /dev/ttyUSB0")
    g.add_argument("--tcp", help="RS485<->TCP bridge host:port, e.g. 192.0.2.182:8899")
    g.add_argument("--replay", help="re-decode a saved capture log (offline)")
    ap.add_argument("--seconds", type=int, default=0, help="stop after N seconds (default: until Ctrl+C)")
    ap.add_argument("--every", type=int, default=10, help="print the inventory every N seconds (0 = only at the end)")
    ap.add_argument("--focus", type=lambda x: int(x, 0), default=None, help="decode only frames for this slave, e.g. 0x06")
    ap.add_argument("--out", default=None, help="capture log path (default: thermia_capture_<timestamp>.log; '-' = none)")
    ap.add_argument("--quiet", action="store_true", help="do not print decoded slot polls, only the inventory")
    a = ap.parse_args()
    out = a.out
    if out is None:
        out = None if a.replay else f"thermia_capture_{time.strftime('%Y%m%d_%H%M%S')}.log"
    elif out == "-":
        out = None
    try:
        src = SerialSource(a.com) if a.com else TcpSource(a.tcp) if a.tcp else ReplaySource(a.replay)
    except Exception as e:
        sys.exit(f"cannot open source: {e}")
    run(src, out, a.seconds, a.every, a.focus, a.quiet)
