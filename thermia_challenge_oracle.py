#!/usr/bin/env python3
"""Challenge-oracle test: ask a real Thermia gateway to answer a FOREIGN challenge.

Context
-------
On the newer iTec controllers (Eco 5, Eco 8) the controller opens a session by
sending a 16-byte challenge to slot 0x0F:

    0f 17 | 0730 0008 | 071c 0008 | 10 | <16 bytes> | CRC

A genuine gateway answers with

    0f 17 | 10 | <16 bytes> | CRC

typically within 30-60 ms, and the controller then starts the configuration
export. An Eco controller VERIFIES that answer: replaying a recorded response
is refused. So the open question is whether the answer depends only on the
challenge, or on the gateway's own pairing/session state.

This script replays challenges recorded from ANOTHER heat pump and reports any
response. Connect the adapter in PARALLEL to the existing bus and change no
wiring: controller, gateway and adapter all on the same pair, exactly how a
passive logger is attached.

Do not isolate the gateway for this test. It answers only when it is ready to
open a session, and that readiness very likely depends on seeing the controller
poll it, so a gateway sitting alone in silence may never answer at all and would
produce a false negative.

Two masters do share the wire, so this script waits for a quiet gap before it
transmits. A collision is not dangerous -- the frame fails CRC and is discarded --
but it would waste the challenge.

What it sends
-------------
Byte for byte what the gateway's own controller sends. It is an FC23 frame, so
it does contain a write to the gateway's own challenge area at 0x071C -- that is
what a challenge is. Nothing is addressed to the heat pump, and no setting of
any kind is written anywhere.

Important: a genuine gateway answers only a small fraction of challenges. In two
recorded sessions only 6 of 163 were answered, because the gateway answers when
it is ready to open a session. A short silent run therefore proves nothing. Let
this run for several minutes, and power-cycle the gateway while it runs, so that
it passes through its start-up state while challenges are arriving.

Usage
-----
    pip install pyserial
    python thermia_challenge_oracle.py --port COM5 --minutes 15

    (Linux/macOS: --port /dev/ttyUSB0)

Power-cycle the gateway once while this runs, so that it passes through its
start-up state while challenges are arriving.
"""

import argparse
import datetime
import sys
import time

try:
    import serial
except ImportError:
    sys.exit("pyserial missing -- run:  pip install pyserial")

# Challenges recorded from an iTec Eco 8 cold start, 2026-09-29. CRC included.
CHALLENGES = [
    "0f1707300008071c0008107e928bcfce5c05ba4e973e617004c2edf39d",
    "0f1707300008071c000810a7d68d50ebeb55e4bc2f26ba23f0a06665f7",
    "0f1707300008071c0008108fc003efddd746c112eb1b7228f12b958810",
    "0f1707300008071c000810c26c4ff6085912ed7e88e52f0f247bc0280b",
    "0f1707300008071c0008102640b87be0a5cd11ceebfb791e51ad56b621",
    "0f1707300008071c00081046cb69b9296eb9f20a3320b88e17f1f4d4b4",
]


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def check_frames() -> list:
    frames = []
    for hexstr in CHALLENGES:
        raw = bytes.fromhex(hexstr)
        body, crc_rx = raw[:-2], raw[-2] | (raw[-1] << 8)
        if crc16(body) != crc_rx:
            sys.exit("built-in frame has a bad CRC -- do not send it: " + hexstr)
        frames.append(raw)
    return frames


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", required=True, help="serial port of the RS485 adapter")
    ap.add_argument("--baud", type=int, default=9600)
    ap.add_argument("--minutes", type=float, default=15.0, help="how long to keep going")
    ap.add_argument("--cadence", type=float, default=4.3, help="seconds between challenges")
    ap.add_argument("--listen", type=float, default=0.4, help="seconds to listen after each one")
    ap.add_argument("--gap", type=float, default=0.08,
                    help="seconds of bus silence required before transmitting")
    ap.add_argument("--gap-wait", type=float, default=3.0,
                    help="give up waiting for a gap after this many seconds")
    ap.add_argument("--log", default=None, help="raw log file (default: auto-named)")
    args = ap.parse_args()

    frames = check_frames()
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    logname = args.log or "challenge_oracle_%s.log" % stamp

    print("port %s, %d baud 8E1, %d challenges, cadence %.1f s, running %.0f min"
          % (args.port, args.baud, len(frames), args.cadence, args.minutes))
    print("log: %s" % logname)
    print("nothing is written to the heat pump; stop any time with Ctrl-C\n")

    sent = 0
    answers = 0
    other = 0
    deadline = time.time() + args.minutes * 60.0

    with serial.Serial(args.port, args.baud, bytesize=8, parity=serial.PARITY_EVEN,
                       stopbits=1, timeout=0.05) as ser, open(logname, "w") as log:
        log.write("# challenge-oracle run %s, port %s\n" % (stamp, args.port))
        idx = 0
        while time.time() < deadline:
            frame = frames[idx % len(frames)]
            nr = idx % len(frames) + 1

            # Wait for a quiet gap: the controller is also master on this pair.
            wait_start = time.time()
            last_byte = time.time()
            while time.time() - wait_start < args.gap_wait:
                chunk = ser.read(64)
                if chunk:
                    log.write("%8.3f BUS %s" % (time.time(), chunk.hex()) + chr(10))
                    last_byte = time.time()
                elif time.time() - last_byte >= args.gap:
                    break

            t0 = time.time()
            ser.write(frame)
            ser.flush()
            sent += 1
            log.write("%8.3f TX ch#%d %s\n" % (t0, nr, frame.hex()))

            buf = bytearray()
            while time.time() - t0 < args.listen:
                chunk = ser.read(64)
                if chunk:
                    buf += chunk
            if buf:
                dt = (time.time() - t0) * 1000.0
                log.write("%8.3f RX %s\n" % (time.time(), buf.hex()))
                if buf[:3] == b"\x0f\x17\x10" and len(buf) >= 21:
                    answers += 1
                    print("*** ANSWER to challenge #%d after %.0f ms: %s"
                          % (nr, dt, buf[3:19].hex()))
                    log.write("# ANSWER payload %s\n" % buf[3:19].hex())
                else:
                    other += 1
                    print("    something on the bus after #%d (%.0f ms): %s"
                          % (nr, dt, buf.hex()))
            else:
                print("    #%d silent" % nr)

            idx += 1
            log.flush()
            rest = args.cadence - (time.time() - t0)
            if rest > 0:
                time.sleep(rest)

    print("\nsent %d, answers %d, other traffic %d" % (sent, answers, other))
    if answers:
        print("The gateway answered a challenge from a different heat pump.")
    else:
        print("No answer. Only meaningful if the run was long and the gateway")
        print("was power-cycled during it -- a real gateway answers only a few")
        print("per cent of challenges, when it is ready to open a session.")
        print("Check the log shows BUS traffic: if the bus looks silent, the")
        print("adapter is not actually hearing the controller and the run is void.")
    print("Please send the log file: %s" % logname)


if __name__ == "__main__":
    main()
