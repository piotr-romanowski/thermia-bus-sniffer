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
- **Online-module slot: transport layer known, application layer not.** Any syntactically valid 12-register
  reply makes the display switch its 0x06 polling from ~4.3 s to an alternating 0.7 / 1.4 s, and it keeps the
  fast cadence for about two minutes after the replies stop. Setting reply word @45002 (`0xAFCA`) to `0x03E8`
  makes the display write 16 to `0x0F` @2145 (`0x0861`); clearing it back to 0 clears that ACK. This is
  ryckema's finding on the XTR M, reproduced here, so it is a platform property. `0x03E8` is a constant, not a
  register number: 1001, 1002, 999 and 1234 give no ACK. Any non-zero value there also turns the display's
  @45020 heartbeat from 32 to 16. The ACK is transport only — a payload of zeros is acknowledged too — and
  no content tried so far — here: zeros, status words, product id, ASCII, echo, RTC, state machines,
  setpoints; on the XTR M (ryckema): register/value pairs, raw Danfoss Link request bodies and more — changes anything or makes the module appear on the display. A full address scan
  (1–247, FC03/FC04) found no hidden slave. The old **Danfoss Link HP-kit (086L2382 / DCM03)** used this slot
  to control DHP-AQ pumps *locally* without any cloud, so it is not cloud-gated by design. **We still need one
  real capture with a working module**; one is being arranged.
- **The display's heartbeat to the online slot (@45020) is the heating-season condition.** Over eleven days it
  pulsed 0 ↔ 32 (15–20 s high, period ~64 s) exactly when the displayed outdoor temperature was below the
  heat-stop setting (hysteresis 2–3 K); it pulsed the same way before the heating season, when the pump
  was only making hot water. Raising and lowering the heat-stop
  limit on the display turned it on within 31 s and off in the same second. It is a copy of bit 5 of `0x02`
  @43022, sent with the next 0x06 poll.
- **The controller counts time in ~63.9 s "minutes".** The mean heartbeat period over 7300 periods is 63.87 s.
  In those units a periodic routine (a 62–68 s state in @43020 followed by a 10-minute window in @43022) repeats
  every 1440 minutes = 25 h 33 min real time, and hot water / heating alternate every 30 minutes (1916–1922 s)
  when both are demanded, matching the display's two 30-minute settings. A prediction made from this was
  met within 117 s one day later. The routine is probably the daily one-minute circulation pump exercise
  described in the DHP-iQ manual; that part is not yet confirmed.

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
independent verification on a different model (iTec Eco 8 vs XTR M) from logged traffic, the output map from the
display's manual test, the `0x1E` @20 bit meanings, and the transmit-side findings.

| Slave | Registers | Meaning | Status |
|---|---|---|---|
| `0x0F` FC16 | @1011 | High power (1 = on) | confirmed (A/B on the display) |
| `0x0F` FC16 | @1053–1059 | Hot-water menu: start temp, run time, top-up interval / stop temp / time, sensor influence %, eco influence % | confirmed (display menu) |
| `0x0F` FC16 | @1090–1102 | Cooling menu: cooling on, desired temp, mode-active limit, time, room sensor, hysteresis low/high (÷10 K) | candidate |
| `0x02` FC23 write | @43020 | controller **output** bitfield, mapped with the display's MANUAL TEST (one output at a time, power meter logged): **bit 0** reversing valve on hot water, **bit 1** cooling by-pass, **bit 2** potential-free aux output, **bit 3** external aux heater, **bit 6** controller active (0 when the operating mode is Off — not "compressor running"), **bit 7** immersion heater stage 1 (+2.7 kW measured). Bit 5 unassigned (seen with the external aux heater). Read compressor state from `0x1E` FC04 @11/13 | confirmed (manual test) |
| `0x02` FC23 write | @43021 | **bit 4** = immersion heater stage 2 (+5.2 kW measured) | confirmed (manual test) |
| `0x1E` FC16 | @1 | target supply temperature ÷10, written by the display to the outdoor unit | confirmed |
| `0x1E` FC04 | @11, 12, 13, 14, 16 | compressor Hz, max-frequency ratio %, current ÷10 A, fan rpm, EEV steps | confirmed |
| `0x1E` FC04 | @20 | outdoor-unit state **bitfield**: **0x01** compressor running, **0x04** outdoor fan turning, **0x08** water flow / circulation pump, **0x10** always set — each bit matched its signal in 100.0 % of ~20 000 samples over seven days. So 16 idle, 24 pump, 28 pump + fan, 29 + compressor; 20 = fan alone (ryckema's "autonomous sequence", *not* defrost). A reverse-cycle defrost should show as 25 (0x19), not yet seen | confirmed here; values and the "20 is not defrost" note from ryckema |
| `0x1E` FC04 | @21 | status bitfield: **0x0020** heating context, **0x0040** hot-water context, **0x0200** outdoor unit active. Observed 577 → 65 → 33 → 545 across one DHW → heating handover (we had @20/@21 mislabelled as superheat / subcooling) | bits from ryckema; confirmed here in both modes |
| `0x1E` FC16 | @4 | mode request: **1** = heating, **2** = hot water (3 = cooling per ryckema, not seen here yet) | enum from ryckema; the 2 → 1 switch confirmed here |
| `0x02` FC23 write | @43023 | **circulation (condenser) pump speed, %**: follows the MANUAL TEST "CONDENSER PUMP" setting step for step (30 → 40 → 50…); 5 = pump stopped, 10 = idle, 80 while a heater runs; the 80 → 100 ramp at the start of a heating cycle is the pump spinning up | confirmed (manual test) |
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

*(Correction: an earlier version described `0x02` @43023 as "a counter that saturates at 100", because it
ramped 80 → 100 while the compressor frequency stayed flat. It is the circulation pump speed — the display's
manual test moves it directly.)*

**Electric heater: now confirmed.** Earlier versions left the heater out because an observation had made us
doubt our mapping. The display's MANUAL TEST settled it with a power meter: stage 1 = @43020 bit 7 (+2.7 kW),
stage 2 = @43021 bit 4 (+5.2 kW). The `0x1E` command registers @2 and @8 did not move with either stage, so
they are not heater controls (@8 is the outdoor unit run enable).

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
