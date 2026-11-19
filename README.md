# thermia-bus-sniffer

Passive sniffer for the internal **RS485 / Modbus RTU** bus of **Thermia / Danfoss** heat pumps
(iTec, iTec Eco, iTec XT/XTR, Atec, Danfoss DHP-AQ; probably Diplomat / Legend — same accessory bus).
One file, no transmit, `pyserial` only.

Why it exists: a few of us are reverse-engineering this bus to integrate the pumps with Home Assistant
without Thermia Connect. A first set of real captures with an online module (an ATEC with a DCM3) now exists in
GitHub discussion 143 of klejejs/ha-thermia-heat-pump-integration, and a first capture from an **iTec with a real
Thermia Online gateway**, cold start included, has been analysed (see thermia-itec-docs) and its owner has been pointed at that
discussion and at the HA Community thread to share the raw log directly. **Still missing:** (1) more of that same
case, ideally with several cold starts in one file, and (2) an **ATEC start with the module disconnected**.
If you own one of those, a capture with this tool would help the whole family of pumps.

## Quick start

1. Any USB-RS485 adapter (≈ 5 €). Connect A and B to the RJ45 "Modbus" port of the indoor unit
   (ports 122/123; a spare port is usually free — the display uses one, the online module the other).
   RJ45 pinout seen on iTec: **1+2 = A, 3+4 = B, 5+6 = GND, 7+8 = +12 V** (do not touch 7/8).
2. `pip install pyserial`
3. `python thermia_bus_sniffer.py --com COM6 --seconds 90` (Linux: `--com /dev/ttyUSB0`)
4. Post / send the file `thermia_capture_<timestamp>.log`. It contains only bus frames (hex), nothing personal.
   The timestamps are seconds since the start of the capture, so **write down the wall-clock time you started it** and the time of every action.
   For a **cold start**: start the sniffer first, power the adapter independently of the heat pump (otherwise the first seconds are lost), then switch the pump off and on.

If you use an RS485↔TCP bridge (e.g. Elfin EE11A) instead: `--tcp <ip>:8899`.

> **Elfin EE11/EW11 users: set the serial CLI to `Disable`.** With the factory setting `CliGetIn = Always`
> (web UI → serial port settings, or `POST /cmd`, section `UART`) a long frame on the bus can drop the
> Elfin into its command console: it then **transmits `\r\nEPORT>` onto the RS485 bus** (corrupting other
> devices' frames) and stops forwarding data to TCP until it is power-cycled. We hit this on an iTec Eco 8
> with the 129-byte `0x0F` page `0884` (2026-10-26); `CliGetIn = Disable` removed it completely.
>
> **Fixed 2026-10-27:** frames longer than 64 bytes that arrive in several TCP chunks (Elfin `BufSize` 64)
> were silently dropped by the frame splitter. If you captured through a TCP bridge before this date, long
> pages (e.g. `0x0F` FC16 `0884/60`) are missing from the decoded output; `--replay` of a raw log is unaffected
> only if the log was written already frame-aligned.
To see only the online-gateway slot decoded: `--focus 0x0F` (legacy pumps: `0x06`). To re-decode a saved log: `--replay file.log`.

## Second tool: challenge-response oracle test (`thermia_challenge_oracle.py`)

**Only useful if you own a working online gateway** (a classic DCM/DCM03, or a Thermia Connect).
Everyone else can ignore this file; the sniffer above is the one you want.

Newer controllers open a session by sending a 16-byte challenge to slot `0x0F`, and a genuine
gateway answers it in 30-60 ms. An iTec Eco controller **verifies** that answer: replaying a
recorded response is refused. So the open question is whether the answer depends only on the
challenge, or also on the gateway's own pairing or session state.

This script replays challenges recorded from **another** heat pump and reports any response.
If your gateway answers a challenge from someone else's pump, the response is portable, and a
second-hand gateway becomes a usable oracle for everyone. If it stays silent, that is worth
knowing too.

```
pip install pyserial
python thermia_challenge_oracle.py --port COM5 --minutes 15
```

How to run it, and why this way:

* **Leave the gateway connected to your controller** and hang the adapter on the bus in
  parallel, like the sniffer. Do not isolate the gateway: it answers only when it is ready to
  open a session, and that readiness plausibly depends on being polled by its controller, so a
  gateway sitting alone in silence may never answer and would produce a false negative.
* Two devices are then transmitting as master on the same pair, so the script waits for a gap
  of bus silence before each frame. A collision is harmless, the frame fails CRC and is
  dropped; it only wastes a challenge.
* **Power-cycle the gateway once while it runs**, so it passes through its start-up state with
  the challenges already arriving.
* **Let it run.** In two genuine captures the gateway answered 6 of 163 challenges, so a short
  silent run means nothing.
* If the log shows no controller traffic at all, the adapter is not hearing the bus: the run is
  void, not negative. The script says so.

Note on what is sent: these are FC23 frames, which write eight words at `071C` while reading
eight from `0730`. That is what a challenge is. Nothing is addressed to the heat pump and no
setting is changed anywhere, but it is not a read-only operation in Modbus terms.

## Protocol and register map → thermia-itec-docs

Everything learned about the bus — framing and value encoding, the slot map, room-sensor emulation on `0x0A`,
the online-gateway session on `0x0F` (pages, `0708` mailbox, write whitelist), status/alarm bitfields, defrost,
heating regulation, the pump calendar — and the **register map with certainty levels** now lives in one place:

- **https://github.com/piotr-romanowski/thermia-itec-docs** — [`PROTOCOL.md`](https://github.com/piotr-romanowski/thermia-itec-docs/blob/master/PROTOCOL.md) and [`REGISTERS.md`](https://github.com/piotr-romanowski/thermia-itec-docs/blob/master/REGISTERS.md)

This repo keeps only the tools and the sample captures. The XTR M map by ryckema
(https://github.com/ryckema/Thermia_itec) addresses the same registers in hex (`0x03F4` = 1012); both maps are
cross-checked.

## What a useful capture contains

Run the sniffer for 60–120 s with the online module / gateway connected and working. On the iTec family the
gateway lives on slot **`0x0F`** (challenge `FC23`, page writes `FC16`, mailbox reads `FC03 @0708`); older Danfoss
Link / DCM03 setups used `0x06`. The interesting lines are the **responses from the slot** (the tool marks them
`<<< SLOT ANSWERED`). Boot captures (power-cycle the pump with the sniffer already running) are even better —
the challenge appears only when the controller opens a session.

## Contributing

Commit messages in this repo carry no AI-attribution footers (`Co-Authored-By: …`, `Generated with …`).
A `commit-msg` hook in `.githooks/` rejects them. Git does not enable repo hooks on clone, so turn it on once:

```
git config core.hooksPath .githooks
```

## License

MIT. Not affiliated with Thermia, Danfoss or Samsung. Passive listening only — you are responsible for what you
connect to your heat pump.
