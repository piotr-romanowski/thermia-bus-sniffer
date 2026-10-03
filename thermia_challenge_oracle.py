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

# Positive control (ryckema's suggestion): a challenge recorded on an ATEC whose
# own DCM03 answered it 29 ms later. The answer it produced that day was
#     20b8f28a236f28fa0339669e2c6f600d
# If your gateway answers this one but none of the Eco 8 challenges above, that is
# a strong negative. If it answers neither, the run is inconclusive rather than
# negative -- it most likely never reached a state where it answers at all.
CONTROL = "0f1707300008071c0008102ee156572b8b84a0bc7a65c72d56185d7840"
CONTROL_EXPECTED = "20b8f28a236f28fa0339669e2c6f600d"


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def find_response(buf: bytes):
    """Find a valid 0f 17 10 <16 bytes> <CRC> anywhere in the buffer.

    On a live bus the reply is not necessarily the first thing we hear: other
    traffic, or the adapter's own echo, can arrive first.
    """
    for i in range(len(buf) - 20):
        if buf[i] == 0x0F and buf[i + 1] == 0x17 and buf[i + 2] == 0x10:
            frame = buf[i:i + 21]
            if len(frame) == 21 and crc16(frame[:-2]) == (frame[-2] | (frame[-1] << 8)):
                return frame[3:19]
    return None


def check_frames() -> list:
    frames = []
    for hexstr in list(CHALLENGES) + [CONTROL]:
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
        control_frame = frames[-1]
        control_hits = 0
        while time.time() < deadline:
            # every seventh frame is the positive control
            is_control = (idx % 7) == 6
            frame = control_frame if is_control else frames[idx % 6]
            nr = "control" if is_control else str(idx % 6 + 1)

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
            log.write("%8.3f TX ch#%s %s" % (t0, nr, frame.hex()) + chr(10))

            buf = bytearray()
            while time.time() - t0 < args.listen:
                chunk = ser.read(64)
                if chunk:
                    buf += chunk
            if buf:
                dt = (time.time() - t0) * 1000.0
                log.write("%8.3f RX %s" % (time.time(), buf.hex()) + chr(10))
                payload = find_response(buf)
                if payload is not None:
                    answers += 1
                    if is_control:
                        control_hits += 1
                    tag = "  (control, expected %s)" % CONTROL_EXPECTED if is_control else ""
                    print("*** ANSWER to challenge %s after %.0f ms: %s%s"
                          % (nr, dt, payload.hex(), tag))
                    log.write("# ANSWER ch#%s payload %s" % (nr, payload.hex()) + chr(10))
                else:
                    other += 1
                    print("    bus traffic after %s (%.0f ms), no reply" % (nr, dt))
            else:
                print("    %s silent" % nr)

            idx += 1
            log.flush()
            rest = args.cadence - (time.time() - t0)
            if rest > 0:
                time.sleep(rest)

    print("")
    print("sent %d, answers %d (control %d), other traffic %d"
          % (sent, answers, control_hits, other))
    if answers > control_hits:
        print("The gateway answered a challenge from a different heat pump.")
    elif control_hits:
        print("It answered only the control challenge, the one its own family")
        print("already answered once. That is a strong negative for portability.")
    else:
        print("No answer. Only meaningful if the run was long and the gateway")
        print("was power-cycled during it -- a real gateway answers only a few")
        print("per cent of challenges, when it is ready to open a session.")
        print("Check the log shows BUS traffic: if the bus looks silent, the")
        print("adapter is not actually hearing the controller and the run is void.")
    print("Please send the log file: %s" % logname)


if __name__ == "__main__":
    main()
