# EG4 GridBOSS — Home Assistant Integration

A fully local, no-cloud Home Assistant integration for the **EG4 GridBOSS**
energy management system. Reads live telemetry directly from the EG4 monitoring
dongle over your local network. No MQTT broker, no separate daemon, no EG4 cloud
account required.

> **Status — Pre-capture draft:** Register addresses are based on Growatt-family
> device precedent and are not yet confirmed for the EG4 GridBOSS. Sensors will
> appear in Home Assistant immediately, but values should be verified against the
> EG4 app display before relying on them. See the
> [Verifying Sensor Values](#5-verify-sensor-values) section.

---

## What you get

One **EG4 GridBOSS** device in Home Assistant with these sensors:

| Sensor | Unit | Visible by default |
|---|---|---|
| PV Power | W | Yes |
| Battery SOC | % | Yes |
| Battery Voltage | V | Yes |
| Battery Current | A | Yes |
| Battery Power | W | Yes |
| Grid Power (+ import / − export) | W | Yes |
| Grid Voltage | V | Yes |
| Grid Frequency | Hz | Yes |
| Load Power | W | Yes |
| Inverter Status | code | Yes |
| Battery Temperature | °C | No (enable manually) |
| Inverter Temperature | °C | No (enable manually) |
| PV1 Voltage | V | No (enable manually) |
| PV1 Current | A | No (enable manually) |

---

## Before you start — network requirement

Home Assistant must be able to reach the dongle on **TCP port 8000**.
Pick whichever option fits your setup:

### Option 1 — Dongle on your home LAN (recommended)

Use the EG4 app to switch the dongle from AP mode into **Station mode** so it
joins your normal Wi-Fi network and gets a regular LAN IP (e.g. `192.168.1.50`).
HA and the dongle are then on the same network — no tricks needed.

> Use the dongle's actual LAN IP when you add the integration (not `10.10.10.1`).

### Option 2 — HA host connects to the dongle's own AP

If you leave the dongle in AP mode (SSID: `EG4_XXXXXX`), your HA host must
join that AP. This works if your HA box has Wi-Fi and you're comfortable
connecting it to the dongle's network instead of your main LAN. Enter
`10.10.10.1` as the dongle IP when adding the integration.

### Option 3 — Dongle on LAN but EG4 app only works on `10.10.10.1`

If you put the dongle on your LAN but the EG4 phone app stops working because
it hard-codes `10.10.10.1`, add a redirect rule on your router or Linux gateway:

```bash
# Run on your router/gateway. Replace 192.168.1.50 with the dongle's LAN IP.
DONGLE_IP=192.168.1.50
sudo iptables -t nat -A PREROUTING -d 10.10.10.1 -p tcp --dport 8000 \
  -j DNAT --to-destination ${DONGLE_IP}:8000
sudo iptables -t nat -A POSTROUTING -d ${DONGLE_IP} -p tcp --dport 8000 \
  -j MASQUERADE
```

The phone app continues working; HA uses the dongle's real LAN IP directly.

---

## Installation

### Step 1 — Install HACS (skip if already installed)

HACS is the Home Assistant Community Store. If you don't have it yet:

1. Open a terminal on your HA host (via the **Terminal & SSH** add-on, or SSH directly).
2. Run the HACS installer:
   ```bash
   wget -O - https://get.hacs.xyz | bash -
   ```
3. Restart Home Assistant: **Settings → System → Restart**.
4. Go to **Settings → Devices & Services → Add Integration**, search for **HACS**,
   and complete the setup (requires a free GitHub account for the OAuth step).

Full HACS docs: https://hacs.xyz/docs/use/download/download/

---

### Step 2 — Add this repository to HACS

1. In Home Assistant, click **HACS** in the left sidebar.
2. Click **Integrations**.
3. Click the **three-dot menu** (⋮) in the top-right corner.
4. Select **Custom repositories**.
5. In the **Repository** field, enter:
   ```
   https://github.com/digitalassetadvisors/casper-test-implementation
   ```
6. Set **Category** to **Integration**.
7. Click **Add**.

---

### Step 3 — Download the integration

1. Still in HACS → Integrations, click **Explore & download repositories**.
2. Search for **EG4 GridBOSS**.
3. Click it, then click **Download** (bottom-right).
4. When prompted, confirm the download.
5. **Restart Home Assistant**: Settings → System → Restart.

---

### Step 4 — Add the integration in Home Assistant

1. Go to **Settings → Devices & Services**.
2. Click **+ Add Integration** (bottom-right).
3. Search for **EG4 GridBOSS** and select it.
4. Fill in the form:

   | Field | What to enter |
   |---|---|
   | **Dongle IP or Hostname** | `10.10.10.1` if on the dongle AP, or the dongle's LAN IP (e.g. `192.168.1.50`) if on your LAN |
   | **TCP Port** | `8000` |
   | **Modbus Unit ID** | `1` (if sensors show "Unknown" later, try `0`) |
   | **Poll Interval (seconds)** | `30` (minimum 10; increase to 60 if the dongle seems stressed) |
   | **Protocol** | `auto` (auto-detects; set to `tcp` or `rtu` if auto fails) |

5. Click **Submit**. Home Assistant will test the connection.
   - **Success:** the EG4 GridBOSS device appears immediately.
   - **"Cannot connect":** see [Troubleshooting](#troubleshooting) below.

---

## Step 5 — Verify sensor values

Because the register addresses have not yet been confirmed by packet capture
for the EG4 GridBOSS specifically, you should spot-check the sensors against
the EG4 app before building any automations on them.

**Quick checks:**

| Sensor | How to verify |
|---|---|
| Battery SOC | Should match the % shown in the EG4 app |
| Battery Voltage | For a 48V LiFePO4 system: expect 44–58 V |
| Grid Voltage | Should be ~120 V (line-neutral) or ~240 V (line-line) |
| Grid Frequency | Should be ~60.00 Hz (US) |
| Load Power | Turn a known appliance (e.g. 1500 W heater) on/off and watch this change |
| PV Power | Cover a solar panel and watch this drop |

**If a value looks completely wrong** (e.g. `5700 V` instead of `57 V`), the
scaling factor is off. Open an issue on this repository with the raw value
shown in HA and what the correct value should be, and it will be fixed.

**If a sensor is stuck at `0` or `Unknown`**, the register address is likely
wrong for your specific GridBOSS firmware. Running the probe tool (below)
will help identify the correct address.

---

## Optional — Probe tool for diagnostics

If sensors aren't reading correctly, the probe tool can dump all register
values so you can find the right addresses by comparing them to the app.

On a Linux machine that can reach the dongle:

```bash
# Install Python (already present on most Linux systems)
python3 eg4_probe.py --host 10.10.10.1 --port 8000 --scan
```

This prints every register value. Compare the output to values shown in the
EG4 app — when a number matches (e.g. battery SOC 85 appears in the output),
you've found the correct register address.

Share the output as a GitHub issue if you need help identifying addresses.

---

## Changing settings after setup

To adjust poll interval, protocol, or unit ID after the integration is running:

1. **Settings → Devices & Services**.
2. Find the **EG4 GridBOSS** integration card and click **Configure**.
3. Update the values and click **Submit**. HA will reload the integration automatically.

---

## Troubleshooting

### "Cannot connect" error during setup

1. Confirm your HA host can reach the dongle:
   - Open **Settings → System → Terminal** (or SSH in) and run:
     ```bash
     curl --max-time 5 http://10.10.10.1:8000/ 2>&1 | head -5
     # Or if dongle is on LAN:
     curl --max-time 5 http://192.168.1.50:8000/ 2>&1 | head -5
     ```
   - If `curl` hangs or says "Connection refused", the network path isn't working.
2. Double-check the IP address — verify the dongle's actual IP in your router's
   client list or in the EG4 app's network settings.
3. Make sure you're not running the EG4 phone app at the same time — some dongles
   only allow one TCP connection at a time.

### All sensors show "Unknown" after setup

The connection worked but register reads are returning errors or empty data.

- Try **Modbus Unit ID `0`** instead of `1`: Settings → Configure the integration.
- Try setting **Protocol to `tcp`** explicitly instead of `auto`.
- Run `eg4_probe.py --scan` (see above) and share the output as an issue.

### Sensors show values but they look wrong

| Symptom | Likely cause | Fix |
|---|---|---|
| Battery voltage reads 5700 instead of 57 | Scale should be 0.01, not 0.1 | Open a GitHub issue with the raw value |
| Grid power reads negative when importing | Sign convention is inverted | Open an issue — easy one-line fix |
| SOC reads 8500 instead of 85 | Wrong register address | Run probe tool and open an issue |
| PV power reads 10× too high | Scale is 0.1 W, should be 1 W | Open an issue |

### Dongle stops responding / EG4 app shows disconnected

The dongle may only handle one client at a time or have a small request queue.

1. Increase poll interval to **60 seconds**: Settings → Configure the integration.
2. Stop using the EG4 phone app while HA is polling (or only use it briefly).
3. If the dongle's own AP disappears after the integration starts, the dongle
   firmware may be crashing on certain Modbus requests. Set Protocol to `tcp`
   explicitly and reduce poll interval to 60s.

---

## How it works (technical summary)

The integration connects to the dongle over TCP and sends standard **Modbus TCP**
read requests (function codes `0x03` and `0x04`). These are the same read-only
commands used to read data from industrial sensors — no writes, no commands,
no configuration changes are ever sent to the inverter. The dongle translates
these requests into queries to the GridBOSS over its RS485 bus and returns
register values. Home Assistant polls this endpoint every 30 seconds (configurable)
and updates all sensor entities.

---

## License

MIT
