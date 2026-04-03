# EG4 GridBOSS — Home Assistant Integration

A fully local, no-cloud Home Assistant custom integration for the **EG4 GridBOSS** energy management system.

Reads live telemetry from the EG4 monitoring dongle's local TCP endpoint (`10.10.10.1:8000`) via Modbus TCP — no MQTT broker, no separate daemon, no cloud dependency.

> **Status: Pre-capture draft.** Register addresses are inferred from Growatt-family device precedent and are marked speculative. Once you complete the packet capture described below, the field map will be updated to confirmed addresses.

---

## Sensors provided

| Sensor | Unit | Notes |
|---|---|---|
| PV Power | W | Total solar generation |
| Battery SOC | % | State of charge |
| Battery Voltage | V | Terminal voltage |
| Battery Current | A | + = charging, − = discharging |
| Battery Power | W | + = charging, − = discharging |
| Grid Power | W | + = importing, − = exporting |
| Grid Voltage | V | |
| Grid Frequency | Hz | |
| Load Power | W | Total house consumption |
| Inverter Status | code | Raw status integer |
| Battery Temperature | °C | (hidden by default) |
| Inverter Temperature | °C | (hidden by default) |
| PV1 Voltage / Current | V / A | (hidden by default) |

---

## Requirements

- Home Assistant 2024.1 or newer
- The EG4 monitoring dongle must be **reachable from your HA host** on TCP port 8000:
  - **Option A:** HA host joins the dongle's Wi-Fi AP (`10.10.10.1`)
  - **Option B:** Dongle is in station mode on your LAN; HA and dongle on same subnet
  - **Option C:** DNAT on your router redirects `10.10.10.1:8000` to the dongle's LAN IP

---

## Installation via HACS

1. In Home Assistant, open **HACS → Integrations**.
2. Click the three-dot menu → **Custom Repositories**.
3. Add this repository URL and select category **Integration**.
4. Search for **EG4 GridBOSS** and click **Download**.
5. Restart Home Assistant.

## Manual Installation

```bash
# In your HA config directory:
git clone https://github.com/digitalassetadvisors/casper-test-implementation /tmp/eg4-repo
cp -r /tmp/eg4-repo/custom_components/eg4_gridboss config/custom_components/
# Restart Home Assistant
```

---

## Configuration

After installation and HA restart:

1. Go to **Settings → Devices & Services → Add Integration**.
2. Search for **EG4 GridBOSS**.
3. Enter:
   - **Dongle IP:** `10.10.10.1` (or the dongle's LAN IP)
   - **Port:** `8000`
   - **Modbus Unit ID:** `1` (try `0` if `1` doesn't work)
   - **Poll Interval:** `30` seconds (minimum 10)
   - **Protocol:** `auto` (tries Modbus TCP first, falls back to RTU-over-TCP)

The integration will test the connection and create the device immediately.

---

## Network Setup Options

### Option A: HA host on the dongle AP

Simplest setup if your HA box has Wi-Fi.

```
[ EG4 Dongle AP ] ←Wi-Fi→ [ HA host ] ←Ethernet→ [ Home LAN ]
```

The HA host joins the dongle's AP (`EG4_XXXXXX`) and uses the default
`10.10.10.1` address. No extra configuration needed.

### Option B: Dongle on LAN (recommended for stability)

Put the dongle in station mode to join your LAN. The EG4 app may still try
to connect to `10.10.10.1`. Add this DNAT rule on your router or Linux gateway:

```bash
# Replace 192.168.1.50 with the dongle's actual LAN IP
DONGLE_IP=192.168.1.50
sudo iptables -t nat -A PREROUTING -d 10.10.10.1 -p tcp --dport 8000 \
  -j DNAT --to-destination ${DONGLE_IP}:8000
sudo iptables -t nat -A POSTROUTING -d ${DONGLE_IP} -p tcp --dport 8000 \
  -j MASQUERADE
```

Then configure the integration with the dongle's actual LAN IP instead of `10.10.10.1`.

---

## Packet Capture (required before confirming register addresses)

See `capture_plan_path_a.md` for full instructions. Short version:

```bash
IFACE=wlan0
sudo tcpdump -i $IFACE -s 0 -w /tmp/eg4_local_connect.pcap \
  host 10.10.10.1 and tcp port 8000

# Open EG4 app → Local Connect → let run 90 seconds → Ctrl+C

tshark -r /tmp/eg4_local_connect.pcap \
  -q -z follow,tcp,hex,0 > /tmp/eg4_stream0_hex.txt
```

Share `eg4_stream0_hex.txt` with the integration maintainer to confirm register addresses.

---

## Probe Tool (for register address validation)

```bash
pip install pyyaml
python3 eg4_probe.py --host 10.10.10.1 --port 8000 --scan
```

Compare printed register values against the EG4 app display to confirm addresses.

---

## Protocol Notes

| What we observe | What it means |
|---|---|
| Port 8000 open, short binary frames | Likely Modbus TCP or RTU-over-TCP |
| `0x0000` bytes at offset 2–3 in each frame | Confirms standard Modbus TCP |
| `0xA5` first byte | SolarmanV5 framing — needs additional work |
| TLS handshake in capture | Encrypted — Path A not viable |

See `decision_note_path_a.md` for full go/no-go criteria.

---

## Troubleshooting

**Integration shows "Cannot connect"**
- Verify HA host can reach `10.10.10.1:8000`: `curl --max-time 5 http://10.10.10.1:8000/`
- Check Wi-Fi association or DNAT rule is active

**All sensors show "Unknown"**
- The register addresses are speculative. Run `eg4_probe.py --scan` and compare to the app.
- Try different unit IDs: `0`, `1`, `17`, `100`

**Values look wildly wrong (e.g. 5700 V instead of 57 V)**
- Scaling is off. Update `scale` in the relevant `value_fn` in `sensor.py`

**Dongle becomes unresponsive after integration starts**
- Increase poll interval to 60s in Options
- The dongle may have a limited connection queue — stop the EG4 app while HA is polling

---

## License

MIT
