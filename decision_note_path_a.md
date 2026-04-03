# Decision Note: Path A — EG4 Dongle Local Connect (`10.10.10.1:8000`)

## Why Path A First

The EG4 monitoring dongle exposes a TCP service on port 8000 when the phone
app uses "Local Connect" mode. Community reports describe dumping
HOLD/INPUT-style registers from this endpoint, which is consistent with
Modbus TCP (or Modbus RTU framed over TCP) — a well-understood, register-
oriented binary protocol with no encryption by default.

This path offers the lowest barrier to a read-only local telemetry bridge:

| Factor | Assessment |
|---|---|
| Protocol complexity | Low — likely standard Modbus TCP (FC 03/04) |
| Encryption likelihood | Low based on community reports of plain reads |
| Hardware needed | Linux laptop on same AP or LAN segment; no custom hardware |
| Read-only safety | Modbus read functions (FC 03/04) cause no side effects |
| Precedent in community | Reports of live register dumps exist |

Path B (RS485 sniffer or direct Modbus tap via USB adapter) would require
additional hardware and physical access to the inverter bus. It is a valid
fallback but is strictly harder to start with.

---

## Inferred Protocol Candidates (pre-capture)

Listed in order of likelihood based on community reports and device ancestry:

### Candidate 1 — Standard Modbus TCP (most likely)
- MBAP header (7 bytes) + PDU
- Function codes 0x03 (Read Holding Registers) and 0x04 (Read Input Registers)
- Unit/slave ID probably 1 or 0
- No authentication layer
- Port 8000 instead of the standard 502 is common for consumer-grade dongles

### Candidate 2 — Modbus RTU over raw TCP
- Same frame structure as RS485 Modbus RTU but transported over TCP
- Includes CRC16 at end of frame; no MBAP header
- Some Growatt-family and SolarmanV5-adjacent dongles use this variant

### Candidate 3 — SolarmanV5 / "Solarman logger" framing
- 11-byte proprietary outer header wrapping an embedded Modbus RTU frame
- Common on port 8899; less common on 8000, but possible
- Can be detected by the 0xA5 start byte and 0x15 end byte in raw frames

### Candidate 4 — HTTP/REST or WebSocket API
- Some newer dongles expose JSON endpoints on port 8000
- Would show as HTTP traffic in a capture
- No Modbus at all; fields are in JSON keys

---

## Path A Failure Conditions (Go/No-Go Criteria)

Path A is a **dead end** and you should ask for Path B design if **any** of
the following are true after capture analysis:

| # | Condition | Evidence to look for |
|---|---|---|
| F1 | **TLS/SSL encryption** | Capture shows TLS ClientHello; data is opaque ciphertext |
| F2 | **Non-replayable session tokens** | Each session begins with a challenge-response or HMAC token that changes every connection |
| F3 | **App-side certificate pinning prevents MitM** | Can't decrypt even with custom CA; relevant only if TLS is present |
| F4 | **Port 8000 is not Modbus-like** | Frames share no recognizable Modbus structure; no 0x03/0x04 function codes; responses are large blobs with no register pattern |
| F5 | **Dongle crashes or resets on probe attempts** | Dongle AP disappears or inverter faults after sending Modbus read requests |
| F6 | **Session must originate from the registered app's device** | Dongle silently drops frames or returns errors for any source IP/MAC other than the paired phone |
| F7 | **Response data is fully static** | All field values are constant across known state changes (load toggle, PV change); implies the registers are not live data |

**Soft warning (not immediate failure):** If the dongle requires a login
handshake (username/password exchange before register reads work), this is a
distinct analysis step, not an immediate dead end — document the handshake and
implement it before declaring failure.

---

## If Path A Fails

Return to this document, note which failure condition(s) were triggered, then
ask for **Path B design**: a USB RS485 adapter connecting directly to the
inverter's Modbus bus, bypassing the dongle entirely. That path requires
physical access but avoids all dongle protocol uncertainty.
