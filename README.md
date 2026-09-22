# thermia-bus-sniffer

Passive sniffer for the internal **RS485 / Modbus RTU** bus of **Thermia / Danfoss** heat pumps
(iTec, iTec Eco, iTec XT/XTR, Atec, Danfoss DHP-AQ; probably Diplomat / Legend — same accessory bus).
One file, no transmit, `pyserial` only.

Why it exists: a few of us are reverse-engineering this bus to integrate the pumps with Home Assistant
without Thermia Connect. **The one thing still missing is a capture of a real online module** (Thermia
Connect / Thermia Online / "Link 2.1 & Online" / Danfoss DCM / Danfoss Link HP-kit) talking to the display.
If you own one of those, a 60-second capture with this tool would unlock remote control for the whole family.

## Quick start

1. Any USB-RS485 adapter (≈ 5 €). Connect A and B to the RJ45 "Modbus" port of the indoor unit
   (ports 122/123; a spare port is usually free — the display uses one, the online module the other).
   RJ45 pinout seen on iTec: **1+2 = A, 3+4 = B, 5+6 = GND, 7+8 = +12 V** (do not touch 7/8).
2. `pip install pyserial`
3. `python thermia_bus_sniffer.py --com COM6 --seconds 90` (Linux: `--com /dev/ttyUSB0`)
4. Post / send the file `thermia_capture_<timestamp>.log`. It contains only bus frames (hex), nothing personal.

If you use an RS485↔TCP bridge (e.g. Elfin EE11A) instead: `--tcp <ip>:8899`.
To see only the online-module slot decoded: `--focus 0x06`. To re-decode a saved log: `--replay file.log`.

## What the bus looks like (facts, measured on iTec Eco 8 / DHP-AQ board, display fw 2.3.0)

- **9600 8E1**, Modbus RTU, CRC16 0xA001. **The display panel is the master**; everything else is a slave.
- Slaves polled every ~4 s (≈ 32 frames per cycle):

| Address | What | Functions |
|---|---|---|
| `0x02` | indoor I/O board, operating data | FC23 read 15 regs @43000 (also answers FC03/FC04 @43000) |
| `0x0F` | indoor board, setpoints written by the display | FC16 @1000… (write-only, never answers reads) |
| `0x1E` | outdoor unit (via COMM KIT) | FC04 @0 (22 sensors, ÷10 °C), FC16 @0 (commands), FC03 readable |
| `0x0A` | **accessory slot: Modbus room sensor** (086U9563) | FC23 read 3 @46000, write 3 @46020 |
| `0x06` | **accessory slot: online module** (Thermia Online 086L1899 / Connect / Danfoss DCM) | FC23 read 12 @45000, write 5 @45020 |

- Raw polls (the last written value is the displayed outdoor temperature):

```
display -> 0x06:  06 17 AF C8 00 0C AF DC 00 05 0A 00 00 00 00 00 00 00 00 00 XX CRC
display -> 0x0A:  0A 17 B3 B0 00 03 B3 C4 00 03 06 00 XX 00 10 00 00 CRC        (XX = outdoor °C, 0x10 = room setpoint)
```

- **Room sensor slot works when emulated:** answer `0A 17 06 <room temp ×10> 00 00 00 00 CRC` (reg1 = reg2 = 0,
  exactly what a genuine 086U9563 returns, e.g. `0A 17 06 00 DD 00 00 00 00 BE A9` = 22.1 °C) and the display
  auto-detects a room sensor and uses the value for room compensation (verified: 18 °C → heating, 28 °C → cooling).
  We now know *why* zeros are correct. The three reply registers are **@46000 = room temperature ×10,
  @46001 = pending setpoint request, @46002 = unused** — so a sensor with nothing to request answers 0.
  The meanings come from ryckema's v26 analysis; @46001 has since been confirmed here by transmitting
  (see below). (An early emulator of ours echoed the pushed setpoint into @46001 by accident; that is a
  bug, not a flag.)
- **Setpoint request works — tested, second control path.** @46001 really is a request channel: put a value
  there and the controller adopts it as the room setpoint. Encoding is **×1** (19 means 19 °C), unlike @46000
  which is ×10. This is cleaner than the older trick of faking the measured room temperature, and it is worth
  knowing that writes to the setpoint *mirrors* (`0x0A` @46021, `0x0F` @1012) do **not** work — the display
  owns those. Measured with the pump heating, 12 °C outside:

  | step | reg1 sent | room setpoint |
  |---|---|---|
  | control | 0 | 18, unchanged |
  | no-op | 18 | 18, unchanged, controller unbothered |
  | request up | 19 | 18 → **19** |
  | request down | 18 | 19 → **18** |
  | repeat | 19 | **19** again |
  | release | 0 | **stays 19** |

  What rules out an echo: every change also appeared in @46021, the *master's own push* back to the sensor
  slot, i.e. the controller rebroadcasting the value as its setpoint. Had it ignored @46001, that word would
  have stayed at 18. Semantics are those of a physical dial — the controller stores the setpoint, @46001 is a
  request to change it, and 0 means "nothing to request", not "clear it". Send it once. Restoring a previous
  value means requesting it; switching the emulation off does not bring it back.

  One trap if you repeat this: make the release step use a value **different** from the panel's original
  setpoint. Our first attempt requested 18 while the panel also said 18, so "latched" and "reverted to panel"
  predicted the same observation and settled nothing.

  Caveat on effect size: throughout the above the supply setpoint stayed at 25.0 °C and the compressor at
  15 Hz, because the supply target was sitting exactly on the heating-curve minimum and absorbing everything.
  The setpoint changes were real; at 12 °C outside they had nowhere to go.
- **Open question — is @46022 an alarm indicator?** The third word of the display's push to the sensor slot
  reads a constant 0 here; ryckema logged 0 and 2. Thermia's catalogue describes the Modbus room sensor as
  displaying an *alarm*, and an alarm that is displayed has to travel display→sensor, which makes that word
  the obvious carrier. Useful if true: pump faults are otherwise only visible by walking to the panel (we have
  had E911, the flow-switch fault, from silted-up filters). **If your pump alarms while you are sniffing,
  please check what @46022 does** — that single observation would settle it.
- **Online-module slot does not activate** with any guessed content of the 12 registers (zeros, status words,
  product id, ASCII, echo, RTC, state machines, setpoints…). A full address scan (1–247, FC03/FC04) found no
  hidden slave, so the module must talk to the display through those 12 registers — format unknown.
  The old **Danfoss Link HP-kit (086L2382 / DCM03)** used this slot to control DHP-AQ pumps *locally* without
  any cloud, so it is not cloud-gated by design. **We need one real capture.**

## Register map (joint effort)

A full **read-only register map with certainty levels** (0x02 operating data, 0x0F setpoints, 0x1E outdoor unit,
SG Ready decoding) lives in **ryckema's ESPHome project for the iTec XTR M**:
https://github.com/ryckema/Thermia_itec — please add new findings there or here, we cross-check both.

**Reading both maps side by side:** that project addresses registers in hex, this one in decimal, and they are
the same numbers — `0xB3B0` = 46000, `0xB3C5` = 46021, `0xA80F` = 43023, `0x03F4` = 1012. What it calls "FC17"
is function code `0x17` = **FC23** (Read/Write Multiple Registers), not Report Slave ID.

What this repo adds (iTec Eco 8 / DHP-AQ board). **Confirmed** = matched against the service display or against
our own logged bus traffic; **candidate** = observed, not yet cross-checked — corrections welcome.

Provenance, so credit lands where it belongs: the register *semantics* for `0x0A` @46000–46002 and the `0x1E` @20
state names (including "20 is an autonomous sequence, not defrost") come from **ryckema's v26 analysis**; the
withdrawal of the earlier "SG Ready room offset" reading of @46022 is likewise his. What is ours is the
independent verification on a different model (iTec Eco 8 vs XTR M) from logged traffic, the `0x02` @43023
behaviour, and the transmit-side findings.

| Slave | Registers | Meaning | Status |
|---|---|---|---|
| `0x0F` FC16 | @1011 | High power (1 = on) | confirmed (A/B on the display) |
| `0x0F` FC16 | @1053–1059 | Hot-water menu: start temp, run time, top-up interval / stop temp / time, sensor influence %, eco influence % | confirmed (display menu) |
| `0x0F` FC16 | @1090–1102 | Cooling menu: cooling on, desired temp, mode-active limit, time, room sensor, hysteresis low/high (÷10 K) | candidate |
| `0x02` FC23 write | @43020 | controller context: **bit 0** (0x01) = hot-water request, **bit 6** (0x40) = compressor running. Seen as 0 → 65 at DHW start → 64 when DHW ends and the compressor keeps going in heating | confirmed (watched a full DHW → heating handover) |
| `0x1E` FC16 | @1 | target supply temperature ÷10, written by the display to the outdoor unit | confirmed |
| `0x1E` FC04 | @11, 12, 13, 14, 16 | compressor Hz, max-frequency ratio %, current ÷10 A, fan rpm, EEV steps | confirmed |
| `0x1E` FC04 | @20 | outdoor-unit operating state: **16** idle, **24** transition, **28/29** heating / DHW, **30/31** cooling, **20** autonomous outdoor-unit sequence (*not* defrost) | enum from ryckema; confirmed here — we logged 29 → 28 → 24 → 16 on our own unit |
| `0x1E` FC04 | @21 | status bitfield: **0x0020** heating context, **0x0040** hot-water context, **0x0200** outdoor unit active. Observed 577 → 65 → 33 → 545 across one DHW → heating handover (we had @20/@21 mislabelled as superheat / subcooling) | bits from ryckema; confirmed here in both modes |
| `0x1E` FC16 | @4 | mode request: **1** = heating, **2** = hot water (3 = cooling per ryckema, not seen here yet) | enum from ryckema; the 2 → 1 switch confirmed here |
| `0x02` FC23 write | @43023 | control/sequencing value: 5 and 10 when idle, ramps **80 → 100** in steps of 1 (~2 min) early in a heating cycle, then holds 100 | confirmed values, meaning open |
| `0x0A` FC23 read | @46000–46002 | the room sensor's reply: room °C ×10, pending setpoint request, unused | confirmed / ryckema |
| `0x0A` FC23 write | @46020–46022 | pushed by the display: outdoor °C, room setpoint °C, third word (0 / 2, meaning unknown) | confirmed / ryckema |

A warning about the `0x1E` FC04 block at **@30–51**: it looks like a copy of @0–21 (@40 lines up with @10,
@50/@51 with @20/@21), but **it is not a live mirror — do not decode it as current state.** It does change,
only far more rarely and out of step: on one day @21 went 577 → 65 → 33 → 545 → 577 → 545 → 577 → 65 while
@51 moved just three times, taking the same values (65, 545, 577) hours later; @40 behaved the same way and
one of its values (250) matched @10's 25.0 °C. So it looks like a delayed or latched copy. The mechanism is
unknown and I have not measured the lag — treat the pairing as unconfirmed.

*(Correction: an earlier version of this README called that block "frozen, a snapshot taken at init". That
was wrong — it came from querying a full day's history in the morning, before the day was over, and reading
a single unchanged value as "never moved". Corrected the same day.)*

One negative result worth recording, since `0x02` @43023 is easy to misread as a modulation level: during a
heating cycle it ramped 80 → 100 while the **compressor frequency sat flat at 38–39 Hz** the whole time, and it
stayed at 100 for the remaining ~27 minutes of the run. So it is not compressor output, load or a percentage —
it behaves like a counter that saturates at 100.

Not listed on purpose: everything about the **electric heater**. We had mapped it to *other* bits of @43020,
to @43021 and to `0x1E` command registers; that reading turned out to be wrong and is still being re-verified,
so none of it is published. (Bits 0 and 6 of @43020 above are a separate, confirmed matter — hot-water request
and compressor, nothing to do with the heater.)

**Transmitting on the bus works** (room-sensor emulation, see above): the display waits ~145 ms for a slave reply,
so a slave only has to keep the ≥3.5-character silence before answering. There is no arbitration problem as long as
you answer *only* polls addressed to your slot and never speak unsolicited.

## What a useful capture contains

Run the sniffer for 60–120 s with the online module connected and working. The interesting lines are the
requests to `0x06` and — above all — the **responses from `0x06`** (the tool marks them `<<< SLOT ANSWERED`).
Boot captures (power-cycle the pump with the sniffer already running) are even better.

## License

MIT. Not affiliated with Thermia, Danfoss or Samsung. Passive listening only — you are responsible for what you
connect to your heat pump.
