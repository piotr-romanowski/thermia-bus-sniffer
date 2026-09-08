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

- **Room sensor slot works when emulated:** answer `0A 17 06 <room temp ×10> 00 10 00 00 CRC` and the display
  auto-detects a room sensor and uses the value for room compensation (verified: 18 °C → heating, 28 °C → cooling).
- **Online-module slot does not activate** with any guessed content of the 12 registers (zeros, status words,
  product id, ASCII, echo, RTC, state machines, setpoints…). A full address scan (1–247, FC03/FC04) found no
  hidden slave, so the module must talk to the display through those 12 registers — format unknown.
  The old **Danfoss Link HP-kit (086L2382 / DCM03)** used this slot to control DHP-AQ pumps *locally* without
  any cloud, so it is not cloud-gated by design. **We need one real capture.**

## What a useful capture contains

Run the sniffer for 60–120 s with the online module connected and working. The interesting lines are the
requests to `0x06` and — above all — the **responses from `0x06`** (the tool marks them `<<< SLOT ANSWERED`).
Boot captures (power-cycle the pump with the sniffer already running) are even better.

## License

MIT. Not affiliated with Thermia, Danfoss or Samsung. Passive listening only — you are responsible for what you
connect to your heat pump.
