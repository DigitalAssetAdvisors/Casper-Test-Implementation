# Packet Capture Plan: Path A — EG4 Dongle Local Connect

## Hardware Setup Options

### Option A (preferred for initial capture): Linux Laptop on Dongle AP

```
[ EG4 GridBOSS / Inverter ]
        |  RS485/CAN
[ EG4 Monitoring Dongle ] <-- acts as AP (SSID: e.g. "EG4_XXXXXX")
        |  Wi-Fi
  ┌─────┴─────┐
  │ Linux     │  ← Your laptop; runs tcpdump; has the EG4 app's
  │ laptop    │    traffic visible because it's on the same AP
  └─────┬─────┘
        |  (separate interface or phone tethered)
   [ Phone with EG4 app ]  ← also on dongle AP
```

Both your laptop and the phone join the dongle's AP.  The dongle is the
AP, so **all traffic between the phone and 10.10.10.1 passes through the
dongle's AP and is visible to any other client on the same AP** — no special
tricks needed; the laptop can capture it directly from its own Wi-Fi interface.

> **Note:** Many Wi-Fi drivers in managed/station mode do not pass frames
> destined for other clients unless you enable monitor mode or use a USB Wi-Fi
> adapter that supports it. See the capture commands below for handling this.

### Option B: Linux Box as AP Bridge / DNAT (if Option A capture is incomplete)

If the dongle's AP isolates clients (blocks client-to-client traffic), use a
Linux box as a Wi-Fi-to-Wi-Fi or Wi-Fi-to-Ethernet bridge that sits between
the phone and the dongle:

```
[ Phone ] --Wi-Fi--> [ Linux box, wlan1: "fake AP" ] --wlan0--> [ Dongle AP ]
                        |
                        +-- runs tcpdump on bridge interface
                        +-- optional: DNAT for IP rewrite
```

This requires two Wi-Fi interfaces on the Linux box (or one + Ethernet).

### Option C: Dongle on Home LAN (simplest capture setup, if dongle allows it)

Put the dongle in "Station" mode so it joins your home LAN instead of acting
as an AP.  The phone then also connects to your home LAN.  The app may hard-
code `10.10.10.1` as the target, so you will need a DNAT rule if the dongle
gets a different IP:

```bash
# Example DNAT on the router/Linux gateway — redirect 10.10.10.1:8000 to
# the dongle's actual LAN IP (replace 192.168.1.50 with the dongle's IP):
iptables -t nat -A PREROUTING -d 10.10.10.1 -p tcp --dport 8000 \
  -j DNAT --to-destination 192.168.1.50:8000

# Also add masquerade so the dongle sees traffic coming from the gateway:
iptables -t nat -A POSTROUTING -d 192.168.1.50 -p tcp --dport 8000 \
  -j MASQUERADE
```

Capture then runs on the Linux gateway's LAN interface, capturing all traffic
between the phone and the dongle's actual IP.

---

## Prerequisites (install these before capture day)

```bash
# Debian/Ubuntu
sudo apt-get update
sudo apt-get install -y tcpdump tshark wireshark-common iw wireless-tools \
                        python3 python3-pip python3-venv net-tools nmap

# Allow non-root tshark (optional, for running tshark as your user)
sudo dpkg-reconfigure wireshark-common   # choose "Yes"
sudo usermod -aG wireshark $USER
# Log out and back in for group to take effect
```

---

## Step-by-Step Capture Commands

### Step 1 — Identify your Wi-Fi interface

```bash
ip link show
# or
iw dev
# Look for the interface connected to the dongle AP (e.g. wlan0)
IFACE=wlan0     # set this to your actual interface name
```

### Step 2 — Verify connectivity to the dongle

```bash
# Ping the dongle (should reply quickly if you're on its AP)
ping -c 4 10.10.10.1

# Check what's listening on port 8000
nmap -sT -p 8000 10.10.10.1
# Expected: 8000/tcp open  (some service name, possibly "http" or "unknown")

# Optional: banner grab to detect if it speaks HTTP
curl -v --max-time 5 http://10.10.10.1:8000/ 2>&1 | head -40
# If you see HTTP/1.x response, it's HTTP-based — note this.
# If curl hangs or gets connection refused/reset, it's likely binary protocol.
```

### Step 3 — Start a clean capture (Option A: monitor mode on same AP)

Some drivers support capturing other clients' traffic only in monitor mode.
Try managed-mode capture first (Step 3a); if you only see your own traffic,
fall back to Step 3b.

#### Step 3a — Managed mode capture (simplest, works if AP doesn't isolate)

```bash
# Capture all traffic to/from 10.10.10.1 on port 8000
sudo tcpdump -i $IFACE -s 0 -w /tmp/eg4_local_connect.pcap \
  host 10.10.10.1 and tcp port 8000

# Keep this running. In another terminal, open the EG4 app and
# perform a Local Connect. Let it run for 60–120 seconds, then Ctrl+C.
```

#### Step 3b — Monitor mode capture (if 3a shows no phone traffic)

```bash
# Bring the interface down, set monitor mode, bring back up
sudo ip link set $IFACE down
sudo iw dev $IFACE set monitor none
sudo ip link set $IFACE up

# Find the channel the dongle AP is on (do this BEFORE switching to monitor)
# Re-run after reverting to managed mode to check:
sudo iwlist $IFACE scan 2>/dev/null | grep -A5 "EG4"
# Note the channel number, e.g. Channel:6

# Lock monitor interface to the AP's channel
sudo iwconfig $IFACE channel 6   # replace 6 with actual channel

# Now capture — in monitor mode you'll see all 802.11 frames; filter to TCP
sudo tcpdump -i $IFACE -s 0 -w /tmp/eg4_monitor_mode.pcap \
  type data and \( host 10.10.10.1 and port 8000 \)
```

> **After capture in monitor mode:** Revert to managed mode:
> ```bash
> sudo ip link set $IFACE down
> sudo iw dev $IFACE set type managed
> sudo ip link set $IFACE up
> # Reconnect to the dongle AP
> ```

### Step 4 — Capture with tshark for real-time protocol hints

Run this in parallel with tcpdump (different terminal) to get live field hints:

```bash
sudo tshark -i $IFACE -f "host 10.10.10.1 and tcp port 8000" \
  -Y "tcp.len > 0" \
  -T fields \
  -e frame.time \
  -e ip.src \
  -e ip.dst \
  -e tcp.len \
  -e data \
  2>/dev/null | tee /tmp/eg4_tshark_log.txt
```

This prints a tab-separated line per TCP segment with the raw payload as hex.
It lets you see data flowing in real time without opening Wireshark.

### Step 5 — Capture a "state change" session

After verifying the basic capture works, do a second capture while
deliberately changing system state to help correlate fields:

```bash
sudo tcpdump -i $IFACE -s 0 -w /tmp/eg4_state_changes.pcap \
  host 10.10.10.1 and tcp port 8000
```

During this capture, perform these actions **in order** (note timestamps):

| Time offset | Action | What to look for |
|---|---|---|
| 0:00 | Start capture, open app Local Connect | Establish baseline |
| 0:30 | Turn on a known load (e.g. 1500W heater) | Load power field should increase |
| 1:00 | Turn load off | Load power should decrease |
| 1:30 | Note current PV conditions (shade panel if possible) | PV power field |
| 2:00 | Note battery charge/discharge state (check app display) | Battery power field direction |
| 2:30 | Stop capture | |

Write down the actual wall-clock timestamps of each action.

### Step 6 — Extract raw TCP payload bytes for analysis

```bash
# Dump all TCP payloads from the pcap as hex+ASCII, grouped by connection
tshark -r /tmp/eg4_local_connect.pcap \
  -Y "tcp.len > 0 and (ip.src == 10.10.10.1 or ip.dst == 10.10.10.1)" \
  -T fields -e frame.number -e ip.src -e ip.dst -e tcp.len -e data \
  2>/dev/null > /tmp/eg4_payloads.tsv

# Also produce a human-readable hex dump grouped by stream
tshark -r /tmp/eg4_local_connect.pcap \
  -q -z follow,tcp,hex,0 \
  2>/dev/null > /tmp/eg4_stream0_hex.txt

# If there are multiple TCP streams, repeat for stream index 1, 2, etc.:
# -z follow,tcp,hex,1   -z follow,tcp,hex,2  etc.
# Find how many streams exist:
tshark -r /tmp/eg4_local_connect.pcap \
  -T fields -e tcp.stream 2>/dev/null | sort -nu
```

### Step 7 — Quick Modbus TCP detection check

```bash
# Check if any frames match Modbus TCP (Protocol ID bytes 0x00 0x00 in bytes 2-3)
tshark -r /tmp/eg4_local_connect.pcap \
  -Y "tcp.len >= 6 and (ip.dst == 10.10.10.1 or ip.src == 10.10.10.1)" \
  -T fields -e frame.number -e data 2>/dev/null | \
  awk '{
    hex=$2
    if (length(hex) >= 12) {
      # bytes 2-3 (0-indexed) = Modbus protocol ID, should be 0000
      proto=substr(hex, 5, 4)
      fc=substr(hex, 15, 2)
      if (proto == "0000") print "Frame " $1 ": Modbus TCP candidate, FC=" fc
    }
  }'
```

---

## Handling Common Problems

### Problem: Phone insists on connecting to 10.10.10.1 but dongle is on LAN

```bash
# On the Linux box that acts as gateway, add a DNAT rule.
# Replace DONGLE_IP with the dongle's actual LAN IP.
DONGLE_IP=192.168.1.50

sudo sysctl -w net.ipv4.ip_forward=1
sudo iptables -t nat -A PREROUTING -d 10.10.10.1 -p tcp --dport 8000 \
  -j DNAT --to-destination ${DONGLE_IP}:8000
sudo iptables -t nat -A POSTROUTING -d ${DONGLE_IP} -p tcp --dport 8000 \
  -j MASQUERADE

# Verify the rule:
sudo iptables -t nat -L -n -v | grep 8000
```

### Problem: Wi-Fi client isolation prevents seeing phone traffic

Use Option B (bridge/AP) or move the dongle to a wired LAN and capture on
the Ethernet interface where you can see all traffic without monitor mode.

### Problem: App uses a different IP when on LAN

```bash
# On Linux, run a fake 10.10.10.1 route that points to the dongle's LAN IP:
sudo ip addr add 10.10.10.1/32 dev lo   # NOT recommended — use DNAT instead

# Better: watch where the app connects with ss/netstat while it tries:
watch -n 1 "ss -tn | grep ':8000\|:80\|:443'"
```

---

## Artifacts to Collect and Send Back

After running the captures, provide the following files:

| File | Contents | How to get it |
|---|---|---|
| `eg4_local_connect.pcap` | Full capture of a normal app local connect session | Step 3a/3b |
| `eg4_state_changes.pcap` | Capture with timed load toggle and state notes | Step 5 |
| `eg4_payloads.tsv` | Tab-separated hex payload dump | Step 6 |
| `eg4_stream0_hex.txt` | Human-readable hex stream follow | Step 6 |
| `nmap_8000.txt` | nmap output for port 8000 | Step 2 |
| `curl_banner.txt` | curl banner grab output | Step 2 |
| `tshark_log.txt` | Real-time tshark output | Step 4 |
| `state_change_log.txt` | Hand-written timestamp log of your actions during Step 5 | Manual |

**Compress and send:**

```bash
tar czf eg4_captures_$(date +%Y%m%d).tar.gz \
  /tmp/eg4_local_connect.pcap \
  /tmp/eg4_state_changes.pcap \
  /tmp/eg4_payloads.tsv \
  /tmp/eg4_stream0_hex.txt \
  /tmp/nmap_8000.txt \
  /tmp/curl_banner.txt \
  /tmp/eg4_tshark_log.txt \
  state_change_log.txt 2>/dev/null || true
ls -lh eg4_captures_*.tar.gz
```

Share the `.tar.gz` or paste the contents of `eg4_stream0_hex.txt` and
`eg4_tshark_log.txt` directly into the chat — those two files alone are
usually enough to identify the protocol.
