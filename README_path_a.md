# README: EG4 GridBOSS Local MQTT Bridge — Path A

Local telemetry bridge for the EG4 GridBOSS system via the monitoring dongle's
TCP endpoint (`10.10.10.1:8000`). No cloud dependency. Read-only.

---

## Repository Contents

| File | Purpose |
|---|---|
| `decision_note_path_a.md` | Justification for Path A; failure/dead-end criteria |
| `capture_plan_path_a.md` | Hardware setup and exact capture commands |
| `eg4_probe.py` | CLI tool for probing and decoding live register data |
| `mqtt_bridge.py` | MQTT daemon for Home Assistant auto-discovery |
| `field_map_path_a.yaml` | Structured register map (speculative until captures confirm) |
| `README_path_a.md` | This file |

---

## Prerequisites

### System Packages

```bash
sudo apt-get install -y python3 python3-pip python3-venv tcpdump tshark nmap
```

### Python Dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install paho-mqtt pyyaml
```

---

## Phase 1: Capture and Protocol Identification

**Do this before running any probe or bridge code.**

Follow `capture_plan_path_a.md` exactly. The short version:

```bash
# 1. Join the dongle AP (or put dongle on LAN — see capture plan)
# 2. Verify connectivity
ping -c 4 10.10.10.1
nmap -sT -p 8000 10.10.10.1

# 3. Start capture
IFACE=wlan0   # your interface
sudo tcpdump -i $IFACE -s 0 -w /tmp/eg4_local_connect.pcap \
  host 10.10.10.1 and tcp port 8000

# 4. Open EG4 app → Local Connect; let it run 60–90s; Ctrl+C

# 5. Extract stream contents for analysis
tshark -r /tmp/eg4_local_connect.pcap \
  -q -z follow,tcp,hex,0 > /tmp/eg4_stream0_hex.txt
cat /tmp/eg4_stream0_hex.txt | head -80
```

**Send the capture files back** so protocol decoding can be done before
the probe scripts are tuned.

---

## Phase 2: Running the Probe

Once capture analysis has confirmed the protocol and at least a few register
addresses, use `eg4_probe.py` to validate live reads.

### Installation Check

```bash
python3 eg4_probe.py --help
```

### Quick Connectivity Test

```bash
# Attempt auto-detect and read 50 input registers starting at 0
python3 eg4_probe.py --host 10.10.10.1 --port 8000 \
  --protocol auto --fc 4 --start 0 --count 50
```

Expected good output:
```
[*] Probing protocol type...
[+] Modbus TCP detected (proto_id=0x0000, length=...)
[+] Using protocol: modbus_tcp

==============================
[*] Poll #1 at 2025-01-15T10:30:00
  [2025-01-15T10:30:00] FC04 (Inpt) @ 0–49
  Raw response (109 bytes): 000100000069011000...
    I0000 (0x0000)  raw=  1024 (0x0400)  signed=   1024  ← inverter_status
    I0001 (0x0001)  raw=  3782 (0x0EC6)  signed=   3782  ← pv1_voltage_0.1V [V] = 378.20V
    ...
```

### Scan All Register Blocks

```bash
python3 eg4_probe.py --host 10.10.10.1 --port 8000 --scan --csv /tmp/scan.csv
```

This reads holding registers 0–149 and input registers 0–199, logs to CSV.

### Correlating State Changes

```bash
# Start continuous poll every 15s while you toggle loads
python3 eg4_probe.py --scan --interval 15 --csv /tmp/correlation.csv
# Turn a known load on/off and watch which register values change
# The CSV timestamps make it easy to correlate offline
```

### Replay a Captured Frame

If you captured a specific frame from the pcap and want to test it:

```bash
python3 eg4_probe.py --raw-frame "0001000000060103000000C8"
```

---

## Phase 3: Updating the Field Map

After capturing and probing, update `field_map_path_a.yaml`:

1. For each register where you see a value, check if it matches the expected
   physical value (e.g., battery SOC at 103 should match the app's display).
2. Change `confidence: speculative` → `confidence: likely` or `confirmed`
   based on your correlation tests.
3. Update `mqtt_bridge.py`'s `TELEMETRY_FIELDS` list to match confirmed addresses.

The key fields to confirm first (in priority order):
1. Battery SOC (`input[103]` — compare directly to app value)
2. Battery voltage (`input[70]` — easy to verify with a multimeter)
3. Grid power (`input[11]` — toggle a known load; value should change)
4. PV power (`input[3:4]` — shade a panel and watch it change)
5. Load power (`input[17:18]` — toggle load; value should change by load wattage)

---

## Phase 4: Running the MQTT Bridge

### Config File (optional but recommended)

Create `config.yaml`:

```yaml
dongle:
  host: 10.10.10.1
  port: 8000
  unit_id: 1
  protocol: tcp        # set explicitly once you've confirmed protocol
  poll_interval: 30    # seconds — 30s is safe; 10s is minimum

mqtt:
  host: 192.168.1.10   # your MQTT broker IP
  port: 1883
  username: your_mqtt_user
  password: your_mqtt_password
  topic_prefix: eg4/gridboss
  ha_discovery_prefix: homeassistant
  retain: true

logging:
  level: INFO
```

### Test Run (foreground)

```bash
python3 mqtt_bridge.py --config config.yaml
# Or with command-line args:
python3 mqtt_bridge.py \
  --host 10.10.10.1 --port 8000 \
  --mqtt-host 192.168.1.10 --mqtt-port 1883 \
  --log-level DEBUG
```

Watch the output; you should see "Published N fields" each poll cycle.
Check in Home Assistant → MQTT → Listen to `eg4/gridboss/#` to verify topics.

### MQTT Topic Reference

| Topic | Units | Notes |
|---|---|---|
| `eg4/gridboss/pv_power_w` | W | Total PV generation |
| `eg4/gridboss/battery_soc_pct` | % | Battery state of charge |
| `eg4/gridboss/battery_voltage_v` | V | Battery terminal voltage |
| `eg4/gridboss/battery_power_w` | W | + = charging, - = discharging |
| `eg4/gridboss/grid_import_export_w` | W | + = importing, - = exporting |
| `eg4/gridboss/grid_voltage_v` | V | Grid input voltage |
| `eg4/gridboss/load_power_w` | W | Total house load |
| `eg4/gridboss/inverter_status` | code | Raw status integer |
| `eg4/gridboss/availability` | online/offline | Bridge health |

---

## Phase 5: Home Assistant Integration

### Option A: MQTT Auto-Discovery (recommended)

The bridge publishes HA auto-discovery payloads automatically on startup.
In Home Assistant, go to **Settings → Devices & Services → MQTT**.
The "EG4 GridBOSS" device should appear automatically with all sensors.

If it doesn't appear: verify your MQTT broker integration is configured in HA
and that the `ha_discovery_prefix` in `config.yaml` matches your broker setting
(default: `homeassistant`).

### Option B: Manual YAML Sensors

Add to `configuration.yaml` (or a sensors include file):

```yaml
mqtt:
  sensor:
    - name: "EG4 PV Power"
      state_topic: "eg4/gridboss/pv_power_w"
      unit_of_measurement: "W"
      device_class: power
      state_class: measurement
      availability_topic: "eg4/gridboss/availability"

    - name: "EG4 Battery SOC"
      state_topic: "eg4/gridboss/battery_soc_pct"
      unit_of_measurement: "%"
      device_class: battery
      state_class: measurement
      availability_topic: "eg4/gridboss/availability"

    - name: "EG4 Battery Power"
      state_topic: "eg4/gridboss/battery_power_w"
      unit_of_measurement: "W"
      device_class: power
      state_class: measurement
      availability_topic: "eg4/gridboss/availability"

    - name: "EG4 Grid Power"
      state_topic: "eg4/gridboss/grid_import_export_w"
      unit_of_measurement: "W"
      device_class: power
      state_class: measurement
      availability_topic: "eg4/gridboss/availability"

    - name: "EG4 Load Power"
      state_topic: "eg4/gridboss/load_power_w"
      unit_of_measurement: "W"
      device_class: power
      state_class: measurement
      availability_topic: "eg4/gridboss/availability"
```

---

## Phase 6: Running as a systemd Service

### Create the service file

```bash
sudo nano /etc/systemd/system/eg4-bridge.service
```

```ini
[Unit]
Description=EG4 GridBOSS MQTT Bridge
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=your_username
WorkingDirectory=/home/your_username/eg4-bridge
ExecStart=/home/your_username/eg4-bridge/venv/bin/python3 \
    /home/your_username/eg4-bridge/mqtt_bridge.py \
    --config /home/your_username/eg4-bridge/config.yaml
Restart=on-failure
RestartSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

### Enable and start

```bash
sudo systemctl daemon-reload
sudo systemctl enable eg4-bridge.service
sudo systemctl start  eg4-bridge.service

# Check status
sudo systemctl status eg4-bridge.service

# Follow logs
sudo journalctl -u eg4-bridge.service -f
```

---

## Troubleshooting

### Bridge can't connect to dongle

```bash
# Verify you're on the right network
ip route get 10.10.10.1
ping 10.10.10.1
nmap -sT -p 8000 10.10.10.1
```

### Bridge connects but publishes garbage values

1. Run `eg4_probe.py --scan` and compare printed values to the app display.
2. If values are wildly wrong, the register addresses in `TELEMETRY_FIELDS`
   need updating — see Phase 3 above.
3. Check scaling: a battery voltage reading of 5700 likely needs 0.01 scaling → 57.00V.

### MQTT topics don't appear in Home Assistant

1. Verify MQTT broker is reachable: `mosquitto_pub -h <broker> -t test -m hi`
2. Check broker credentials.
3. Check `ha_discovery_prefix` matches HA's MQTT integration discovery prefix.
4. Restart HA MQTT integration or reload configuration.

### Dongle stops responding after probe

1. Reduce poll frequency (increase `poll_interval`).
2. Reduce number of registers read per request.
3. Check if the dongle's own communication with the inverter is being starved —
   back off to 60s poll interval if the inverter shows any unusual behavior.
4. If the dongle requires a session/login, this will appear in the capture as
   a fixed preamble before register reads. See `decision_note_path_a.md`.

---

## Path A Dead-End Checklist

Consult `decision_note_path_a.md` for the full list. Summary:

- [ ] Port 8000 is open and responds to TCP connections
- [ ] Response to Modbus TCP probe is not TLS-encrypted
- [ ] Response contains recognizable Modbus structure (not opaque blob)
- [ ] At least one register value changes when known physical state changes
- [ ] Dongle does not crash or reset after read probes

If any of these fail, stop and request Path B (RS485 direct tap) design.
