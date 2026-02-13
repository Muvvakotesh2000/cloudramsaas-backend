#!/bin/bash

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Function to print colored output
print_status() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

print_header() {
    echo -e "${BLUE}========================================${NC}"
    echo -e "${BLUE}$1${NC}"
    echo -e "${BLUE}========================================${NC}"
}

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    print_error "This script needs sudo privileges. Please run with sudo or as root."
    exit 1
fi

print_header "Wi-Fi Roaming Simulation - Complete Setup & Dashboard"
echo "Setting up complete Wi-Fi roaming simulation with dashboard"
echo ""

# Phase 1: Clean Environment
print_header "Phase 1: Environment Cleanup"

print_status "Stopping existing services and processes..."
systemctl stop roaming.service 2>/dev/null || true
systemctl stop failover.service 2>/dev/null || true
systemctl stop scone-dashboard.service 2>/dev/null || true
pkill -f hostapd || true
pkill -f wpa_supplicant || true
pkill -f dnsmasq || true
pkill -f dhclient || true
pkill -f roaming_manager || true
pkill -f failover || true
pkill -f "python.*app.py" || true

print_status "Removing existing virtual interfaces..."
ip link set wlan0 down 2>/dev/null || true
ip link set wlan1 down 2>/dev/null || true
ip link set wlan2 down 2>/dev/null || true
ip link set br0 down 2>/dev/null || true
ip link delete br0 2>/dev/null || true
modprobe -r mac80211_hwsim 2>/dev/null || true

# Phase 2: Package Installation
print_header "Phase 2: Installing Required Packages"

print_status "Updating package repositories..."
apt update -qq

print_status "Installing networking packages..."
apt install -y -qq \
    net-tools \
    wireless-tools \
    wpasupplicant \
    hostapd \
    dnsmasq \
    iproute2 \
    iptables \
    bridge-utils \
    iptables-persistent \
    netfilter-persistent \
    python3 \
    python3-pip \
    curl

print_status "Installing Python packages..."
pip3 install flask > /dev/null 2>&1

# Phase 3: Virtual Interface Creation
print_header "Phase 3: Creating Virtual Wi-Fi Interfaces"

print_status "Loading mac80211_hwsim module with 3 radios..."
modprobe mac80211_hwsim radios=3
sleep 2

print_status "Creating bridge interface..."
ip link add name br0 type bridge
ip link set br0 up

print_status "Bringing interfaces up..."
ip link set wlan0 up
ip link set wlan1 up
ip link set wlan2 up
sleep 2

print_status "Adding APs to bridge..."
ip link set wlan0 master br0
ip link set wlan1 master br0

print_status "Configuring IP address for bridge..."
ip addr add 192.168.1.1/24 dev br0

# Phase 4: Access Point Configuration
print_header "Phase 4: Configuring Access Points"

mkdir -p /etc/hostapd

print_status "Creating AP1 configuration (TestNet)..."
cat > /etc/hostapd/ap1.conf << 'EOF'
interface=wlan0
ssid=TestNet
hw_mode=g
channel=1
wpa=2
wpa_passphrase=password123
wpa_key_mgmt=WPA-PSK
# remove: wpa_pairwise=TKIP
rsn_pairwise=CCMP
EOF

print_status "Creating AP2 configuration (TestNet)..."
cat > /etc/hostapd/ap2.conf << 'EOF'
interface=wlan1
ssid=TestNet
hw_mode=g
channel=6
wpa=2
wpa_passphrase=password123
wpa_key_mgmt=WPA-PSK
# remove: wpa_pairwise=TKIP
rsn_pairwise=CCMP
EOF

# Phase 5: DHCP Configuration
print_header "Phase 5: Configuring DHCP Services"

print_status "Creating DHCP configuration for bridge network..."
cat > /etc/dnsmasq.conf << 'EOF'
interface=br0
bind-interfaces
dhcp-range=192.168.1.10,192.168.1.100,255.255.255.0,12h
dhcp-option=3,192.168.1.1
dhcp-option=6,8.8.8.8
port=0
EOF

# Phase 6: Client Configuration
print_header "Phase 6: Configuring Wi-Fi Client"

mkdir -p /etc/wpa_supplicant
cat > /etc/wpa_supplicant/wpa_supplicant.conf << 'EOF'
ctrl_interface=/var/run/wpa_supplicant
update_config=1
country=US

# Make the client proactively evaluate neighbors and move sooner
ap_scan=1
fast_reauth=1
bgscan="simple:3:-65:5"

network={
    ssid="TestNet"
    psk="password123"
    scan_ssid=1
}
EOF

# Phase 7: Create Management Scripts
print_header "Phase 7: Creating Management Scripts"

print_status "Creating roaming manager script..."
cat > /home/koti/roaming_manager.sh << 'EOF'
#!/bin/bash
set -euo pipefail

# ===== Config =====
IFACE="wlan2"
SSID_ALLOW_REGEX="^TestNet$"
THRESHOLD_DBM=-65          # treat below this as weak
MIN_RSSI_IMPROVEMENT=2     # roam if candidate improves by >= this many dB
SCAN_INTERVAL=10           # seconds between loops
SCAN_WAIT_MAX=6            # seconds to wait for scan_results

log_msg() { echo "$(date '+%Y-%m-%d %H:%M:%S') $1"; }

# "signal: -43 dBm" -> "-43"
get_current_signal() {
  local v
  v="$(iw "$IFACE" link 2>/dev/null | sed -n 's/.*signal:[[:space:]]\(-\{0,1\}[0-9]\+\).*/\1/p')"
  [ -n "$v" ] && echo "$v" || echo ""
}

# "Connected to aa:bb:..." -> "aa:bb:..."
get_current_bssid() {
  iw "$IFACE" link 2>/dev/null | awk '/Connected to/ {print $3}' || echo ""
}

# Robustly pick the first numeric NET_ID from list_networks
get_net_id() {
  wpa_cli -i "$IFACE" list_networks 2>/dev/null \
    | awk 'NR>1 && $1 ~ /^[0-9]+$/ {print $1; exit}'
}

# Run a scan, then poll up to SCAN_WAIT_MAX seconds until results are ready
do_scan_and_get_results() {
  local t=0
  local out
  # Kick scan (may reply OK or FAIL-BUSY)
  wpa_cli -i "$IFACE" scan >/dev/null 2>&1 || true
  sleep 1
  while [ $t -lt $SCAN_WAIT_MAX ]; do
    out="$(wpa_cli -i "$IFACE" scan_results 2>/dev/null)"
    # scan_results has a 1-line header; >1 lines means we have data
    if [ "$(printf '%s\n' "$out" | wc -l)" -gt 1 ]; then
      printf '%s\n' "$out" | tail -n +2
      return 0
    fi
    sleep 1; t=$((t+1))
  done
  return 1
}

perform_roaming_check() {
  local current_rssi current_bssid scan_results best_bssid best_rssi best_freq current_ap_rssi
  current_rssi="$(get_current_signal)"
  current_bssid="$(get_current_bssid)"
  log_msg "Current: RSSI=${current_rssi:-NA} dBm | BSSID=${current_bssid:-NA}"

  if ! scan_results="$(do_scan_and_get_results)"; then
    log_msg "Scan produced no results (driver busy?)."
    return
  fi

  best_bssid=""; best_rssi=-999; best_freq=""; current_ap_rssi=-999

  log_msg "Neighbors (matching $SSID_ALLOW_REGEX):"
  while IFS=$'\t' read -r bssid freq signal flags ssid; do
    [ -z "${bssid:-}" ] && continue
    if echo "$ssid" | grep -Eq "$SSID_ALLOW_REGEX" 2>/dev/null; then
      log_msg "  $ssid  $bssid  ${signal} dBm  ${freq} MHz"
      [ "$bssid" = "$current_bssid" ] && current_ap_rssi="$signal"
      if [ "$signal" -gt "$best_rssi" ]; then
        best_rssi="$signal"; best_bssid="$bssid"; best_freq="$freq"
      fi
    fi
  done <<< "$scan_results"

  log_msg "Best candidate: $best_bssid  (${best_rssi} dBm @ ${best_freq} MHz)"

  # Decide
  local should_roam=false reason="" improvement
  if [ -z "${current_rssi:-}" ]; then
    should_roam=true; reason="Disconnected → connect to best"
  elif [ -n "$best_bssid" ] && [ "$best_bssid" != "$current_bssid" ]; then
    [ "$current_ap_rssi" -gt -999 ] && current_rssi="$current_ap_rssi"
    improvement=$(( best_rssi - current_rssi ))  # both negative; -30 - (-45) = +15
    if [ "$improvement" -ge "$MIN_RSSI_IMPROVEMENT" ]; then
      should_roam=true; reason="Better AP (+${improvement} dB)"
    elif [ "$current_rssi" -lt "$THRESHOLD_DBM" ]; then
      should_roam=true; reason="Weak current (${current_rssi} dBm)"
    fi
  fi

  if [ "$should_roam" = true ] && [ -n "$best_bssid" ]; then
    local NET_ID
    NET_ID="$(get_net_id)"
    if [ -z "$NET_ID" ]; then
      log_msg "❌ No NET_ID (check wpa_supplicant & network config)."
      return
    fi

    log_msg "ROAMING: $reason → $best_bssid (${best_rssi} dBm) [NET_ID=$NET_ID]"
    # Prefer the documented BSSID command (pins preferred BSSID for that network)
    # Ref: Hostap/w1.fi ctrl_iface docs: LIST_NETWORKS, BSSID, SET_NETWORK, etc.
    if wpa_cli -i "$IFACE" bssid "$NET_ID" "$best_bssid" | grep -q '^OK'; then
      wpa_cli -i "$IFACE" reassociate >/dev/null 2>&1 || true
      sleep 2
      local new_bssid new_rssi
      new_bssid="$(get_current_bssid)"
      new_rssi="$(get_current_signal)"
      if [ "$new_bssid" = "$best_bssid" ]; then
        log_msg "✅ Now on $new_bssid (${new_rssi:-NA} dBm)"
      else
        log_msg "⚠️ Reassoc did not land on target (now $new_bssid)"
      fi
    else
      log_msg "❌ wpa_cli bssid command failed"
    fi
  else
    log_msg "No roam needed."
  fi
}

log_msg "🚀 Roaming Manager started"
log_msg "Iface=$IFACE | SSID~/$SSID_ALLOW_REGEX/ | Weak<${THRESHOLD_DBM} dBm | Δ≥${MIN_RSSI_IMPROVEMENT} dB | Interval=${SCAN_INTERVAL}s"

while true; do
  perform_roaming_check
  sleep "$SCAN_INTERVAL"
done
EOF

chmod +x /home/koti/roaming_manager.sh

# Phase 8: Create Dashboard
print_header "Phase 8: Creating Web Dashboard"

mkdir -p /home/koti/dashboard
cat > /home/koti/dashboard/app.py << 'EOF'
from flask import Flask, render_template_string
import subprocess
import time
import re

app = Flask(__name__)

def sh(cmd):
    try:
        return subprocess.getoutput(cmd)
    except:
        return "Error executing command"

def get_signal_strength():
    try:
        link_info = sh("iw wlan2 link")
        for line in link_info.split('\n'):
            if 'signal:' in line:
                return line.split('signal:')[1].strip()
        return "Not connected"
    except:
        return "Error"

def get_connected_ap_info():
    """Get detailed info about which AP we're connected to"""
    try:
        # Get current BSSID
        link_info = sh("iw wlan2 link")
        current_bssid = ""
        current_freq = ""
        
        for line in link_info.split('\n'):
            if 'Connected to' in line:
                current_bssid = line.split('Connected to')[1].split()[0]
            elif 'freq:' in line:
                current_freq = line.split('freq:')[1].strip().split()[0] + " MHz"
        
        if not current_bssid:
            return "Not connected", "", ""
        
        # Get AP interface mapping by checking which hostapd process uses which BSSID
        hostapd_info = sh("ps aux | grep hostapd | grep -v grep")
        ap_interfaces = []
        
        # Get BSSIDs for each AP
        ap1_bssid = ""
        ap2_bssid = ""
        
        # Try to get BSSID from hostapd or interface directly
        try:
            ap1_info = sh("iw dev wlan0 info")
            for line in ap1_info.split('\n'):
                if 'addr' in line:
                    ap1_bssid = line.split('addr')[1].strip()
                    break
        except:
            pass
            
        try:
            ap2_info = sh("iw dev wlan1 info")
            for line in ap2_info.split('\n'):
                if 'addr' in line:
                    ap2_bssid = line.split('addr')[1].strip()
                    break
        except:
            pass
        
        # Determine which AP we're connected to
        connected_ap = "Unknown AP"
        if current_bssid == ap1_bssid:
            connected_ap = "AP1 (wlan0)"
        elif current_bssid == ap2_bssid:
            connected_ap = "AP2 (wlan1)"
        else:
            # Fallback: try to determine by frequency/channel
            if "2412" in current_freq or "2417" in current_freq:  # Channel 1
                connected_ap = "AP1 (wlan0) - Channel 1"
            elif "2437" in current_freq or "2442" in current_freq:  # Channel 6
                connected_ap = "AP2 (wlan1) - Channel 6"
        
        return connected_ap, current_bssid, current_freq
        
    except Exception as e:
        return f"Error: {str(e)}", "", ""

def get_ap_status():
    """Get status of both access points"""
    ap_status = {}
    
    # Check AP1 (wlan0)
    ap1_info = sh("iw dev wlan0 info")
    ap1_running = "UP" in ap1_info and "AP" in ap1_info
    
    # Check AP2 (wlan1)  
    ap2_info = sh("iw dev wlan1 info")
    ap2_running = "UP" in ap2_info and "AP" in ap2_info
    
    # Get hostapd process info
    hostapd_processes = sh("ps aux | grep hostapd | grep -v grep")
    ap1_hostapd = "ap1.conf" in hostapd_processes
    ap2_hostapd = "ap2.conf" in hostapd_processes
    
    return {
        'ap1': {'interface_up': ap1_running, 'hostapd_running': ap1_hostapd, 'info': ap1_info},
        'ap2': {'interface_up': ap2_running, 'hostapd_running': ap2_hostapd, 'info': ap2_info}
    }

@app.route("/")
def index():
    link = sh("iw wlan2 link")
    route = sh("ip route | head -5")
    ip_info = sh("ip -4 addr show wlan2 | grep 'inet ' || echo 'No IP assigned'")
    status = sh("wpa_cli -i wlan2 status")
    signal = get_signal_strength()
    
    # Get enhanced AP connection info
    connected_ap, current_bssid, current_freq = get_connected_ap_info()
    
    # Get current SSID
    current_ssid = "Not connected"
    for line in status.split('\n'):
        if line.startswith('ssid='):
            current_ssid = line.split('=')[1]
            break
    
    # Get interface states
    interface_info = sh("ip link show | grep wlan")
    
    # Get AP status
    ap_status = get_ap_status()
    
    # Get scan results to show available APs
    scan_results = sh("wpa_cli -i wlan2 scan_results | tail -n +2")
    
    # Parse scan results to show signal strength of both APs
    ap_signals = {"AP1": "N/A", "AP2": "N/A"}
    for line in scan_results.split('\n'):
        if 'TestNet' in line:
            parts = line.split('\t')
            if len(parts) >= 5:
                bssid, freq, signal_dbm = parts[0], parts[1], parts[2]
                # Determine which AP based on frequency
                if freq.startswith('2412') or freq.startswith('2417'):  # Channel 1
                    ap_signals["AP1"] = f"{signal_dbm} dBm"
                elif freq.startswith('2437') or freq.startswith('2442'):  # Channel 6  
                    ap_signals["AP2"] = f"{signal_dbm} dBm"
    
    template = '''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Wi-Fi Roaming Dashboard</title>
        <meta http-equiv="refresh" content="5">
        <style>
            body { font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }
            .container { max-width: 1200px; margin: 0 auto; }
            .header { background: #2c3e50; color: white; padding: 20px; border-radius: 5px; margin-bottom: 20px; }
            .status-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px; }
            .status-box { background: white; padding: 15px; border-radius: 5px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
            .status-box h3 { margin-top: 0; color: #2c3e50; border-bottom: 2px solid #3498db; padding-bottom: 10px; }
            pre { background: #ecf0f1; padding: 10px; border-radius: 3px; overflow-x: auto; font-size: 12px; }
            .connected { color: #27ae60; font-weight: bold; }
            .disconnected { color: #e74c3c; font-weight: bold; }
            .warning { color: #f39c12; font-weight: bold; }
            .ap-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 15px; margin-bottom: 15px; }
            .ap-box { background: #ecf0f1; padding: 10px; border-radius: 5px; border-left: 4px solid #3498db; }
            .ap-active { border-left-color: #27ae60; background: #d5f4e6; }
            .ap-inactive { border-left-color: #e74c3c; background: #fdf2f2; }
            .current-connection { background: #e8f5e8; border: 2px solid #27ae60; padding: 15px; border-radius: 5px; margin-bottom: 15px; }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>🌐 Wi-Fi Roaming Simulation Dashboard</h1>
                <p>Real-time monitoring of roaming system</p>
                <p>Time: {{ time }}</p>
            </div>
            
            <div class="status-grid">
                <div class="status-box">
                    <h3>📡 Current Connection</h3>
                    <div class="current-connection">
                        <p><strong>🎯 Connected to:</strong> 
                            <span class="{{ 'connected' if connected_ap != 'Not connected' else 'disconnected' }}">
                                {{ connected_ap }}
                            </span>
                        </p>
                        <p><strong>📶 Signal:</strong> <span class="warning">{{ signal }}</span></p>
                        <p><strong>🔗 BSSID:</strong> {{ current_bssid or 'N/A' }}</p>
                        <p><strong>📻 Frequency:</strong> {{ current_freq or 'N/A' }}</p>
                        <p><strong>🌐 SSID:</strong> 
                            <span class="{{ 'connected' if current_ssid not in ['Not connected', ''] else 'disconnected' }}">
                                {{ current_ssid if current_ssid else 'Not connected' }}
                            </span>
                        </p>
                    </div>
                </div>
                
                <div class="status-box">
                    <h3>🏢 Access Points Status</h3>
                    <div class="ap-grid">
                        <div class="ap-box {{ 'ap-active' if ap_status.ap1.interface_up and ap_status.ap1.hostapd_running else 'ap-inactive' }}">
                            <h4>AP1 (wlan0) - Channel 1</h4>
                            <p><strong>Signal:</strong> {{ ap_signals.AP1 }}</p>
                        </div>
                        <div class="ap-box {{ 'ap-active' if ap_status.ap2.interface_up and ap_status.ap2.hostapd_running else 'ap-inactive' }}">
                            <h4>AP2 (wlan1) - Channel 6</h4>
                            <p><strong>Signal:</strong> {{ ap_signals.AP2 }}</p>
                        </div>
                    </div>
                </div>
                
                <div class="status-box">
                    <h3>📋 WPA Supplicant Status</h3>
                    <pre>{{ status }}</pre>
                </div>
                
                <div class="status-box">
                    <h3>🔗 Interface Details</h3>
                    <pre>{{ interface_info }}</pre>
                    <h4>Link Information:</h4>
                    <pre>{{ link }}</pre>
                </div>
                
                <div class="status-box">
                    <h3>🌍 IP Configuration</h3>
                    <pre>{{ ip_info }}</pre>
                </div>
                
                <div class="status-box">
                    <h3>🛣️ Routing Table</h3>
                    <pre>{{ route }}</pre>
                </div>
                
                <div class="status-box">
                    <h3>📡 Available Networks</h3>
                    <pre>{{ scan_results }}</pre>
                </div>
            </div>
            
            <div style="text-align: center; margin-top: 20px; color: #7f8c8d;">
                <p>Auto-refresh every 5 seconds | Dashboard running on port 5000</p>
                <p>🧪 Test roaming by stopping/starting APs or adjusting signal strength</p>
            </div>
        </div>
    </body>
    </html>
    '''
    
    return render_template_string(template, 
                                link=link, 
                                route=route, 
                                ip_info=ip_info, 
                                status=status,
                                signal=signal,
                                current_ssid=current_ssid,
                                connected_ap=connected_ap,
                                current_bssid=current_bssid,
                                current_freq=current_freq,
                                time=time.strftime("%Y-%m-%d %H:%M:%S"),
                                interface_info=interface_info,
                                ap_status=ap_status,
                                ap_signals=ap_signals,
                                scan_results=scan_results)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
EOF

# Phase 9: Create Systemd Services
print_header "Phase 9: Creating System Services"

print_status "Creating roaming service..."
cat > /etc/systemd/system/roaming.service << 'EOF'
[Unit]
Description=Wi-Fi Roaming Manager
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/home/koti/roaming_manager.sh
Restart=always
RestartSec=2
StandardOutput=append:/home/koti/roaming.log
StandardError=append:/home/koti/roaming.log

[Install]
WantedBy=multi-user.target
EOF

print_status "Creating dashboard service..."
cat > /etc/systemd/system/scone-dashboard.service << 'EOF'
[Unit]
Description=Wi-Fi Roaming Dashboard
After=network-online.target

[Service]
WorkingDirectory=/home/koti/dashboard
ExecStart=/usr/bin/python3 /home/koti/dashboard/app.py
Restart=always
User=root
Group=root

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload

# Phase 10: Start All Services
print_header "Phase 10: Starting All Services"

print_status "Stopping default dnsmasq service..."
systemctl stop dnsmasq 2>/dev/null || true
systemctl disable dnsmasq 2>/dev/null || true

print_status "Starting access points..."
hostapd -B /etc/hostapd/ap1.conf
hostapd -B /etc/hostapd/ap2.conf
sleep 3

print_status "Starting DHCP server..."
pkill dnsmasq || true
sleep 1
dnsmasq -C /etc/dnsmasq.conf --pid-file=/var/run/dnsmasq.pid &
sleep 3

print_status "Starting Wi-Fi client..."
wpa_supplicant -B -i wlan2 -c /etc/wpa_supplicant/wpa_supplicant.conf
sleep 3

print_status "Scanning for networks..."
wpa_cli -i wlan2 scan > /dev/null
sleep 3

print_status "Available networks:"
wpa_cli -i wlan2 scan_results

print_status "Connecting to TestNet..."
sleep 5

print_status "Getting IP address..."
timeout 30 dhclient -v wlan2 &
DHCP_PID=$!
sleep 15

# Kill DHCP if still running
if kill -0 $DHCP_PID 2>/dev/null; then
    kill $DHCP_PID 2>/dev/null || true
fi

# If no IP, retry
if ! ip addr show wlan2 | grep -q "inet.*192.168"; then
    print_warning "No IP assigned, retrying connection..."
    wpa_cli -i wlan2 reassociate > /dev/null
    sleep 5
    timeout 20 dhclient -v wlan2 || true
fi

# Phase 11: Enable Services
print_header "Phase 11: Enabling System Services"

systemctl enable roaming.service
systemctl enable scone-dashboard.service

print_status "Starting services..."
systemctl start roaming.service
systemctl start scone-dashboard.service

# Phase 12: Dashboard Startup
print_header "Phase 12: Starting Dashboard"

print_status "Stopping any existing dashboard processes..."
pkill -f "python.*app.py" || true
sleep 2

print_status "Starting dashboard on port 5000..."
cd /home/koti/dashboard
nohup python3 app.py > dashboard.log 2>&1 &
DASHBOARD_PID=$!
sleep 5

# Phase 13: Final Status and Fixes
print_header "Phase 13: Final Status Check"

print_status "Interface Status:"
echo "  br0: $(ip addr show br0 | grep 'inet ' | awk '{print $2}' || echo 'No IP')"
echo "  wlan2: $(ip addr show wlan2 | grep 'inet ' | awk '{print $2}' || echo 'No IP')"

print_status "Process Status:"
echo "  hostapd: $(ps aux | grep hostapd | grep -v grep | wc -l) processes"
echo "  dnsmasq: $(ps aux | grep dnsmasq | grep -v grep | wc -l) processes"
echo "  wpa_supplicant: $(ps aux | grep wpa_supplicant | grep -v grep | wc -l) processes"

print_status "Wi-Fi Client Status:"
wpa_cli -i wlan2 status || echo "wpa_cli failed"

# Auto-fix common issues
print_status "Auto-fixing common issues..."

# Ensure bridge has correct IP
if ! ip addr show br0 | grep -q "192.168.1.1"; then
    ip addr flush dev br0 2>/dev/null || true
    ip addr add 192.168.1.1/24 dev br0
    ip link set br0 up
fi

# Restart DHCP if no processes
if ! ps aux | grep dnsmasq | grep -v grep > /dev/null; then
    print_warning "Restarting DHCP server..."
    dnsmasq -C /etc/dnsmasq.conf --pid-file=/var/run/dnsmasq.pid &
    sleep 2
fi

# Force reconnection if no IP
if ! ip addr show wlan2 | grep -q "inet.*192.168"; then
    print_warning "Client has no IP, forcing reconnection..."
    pkill dhclient || true
    pkill wpa_supplicant || true
    sleep 2
    
    wpa_supplicant -B -i wlan2 -c /etc/wpa_supplicant/wpa_supplicant.conf
    sleep 3
    wpa_cli -i wlan2 scan > /dev/null
    sleep 3
    sleep 5
    dhclient wlan2 &
    sleep 10
fi

# Check dashboard
if kill -0 $DASHBOARD_PID 2>/dev/null; then
    print_status "✅ Dashboard is running (PID: $DASHBOARD_PID)"
else
    print_warning "Dashboard may have failed, restarting..."
    cd /home/koti/dashboard
    nohup python3 app.py > dashboard.log 2>&1 &
    sleep 3
fi

# Final Summary
print_header "🎉 Setup Complete!"

SERVER_IP=$(hostname -I | awk '{print $1}')
echo ""
echo -e "${GREEN}✅ Wi-Fi Roaming Simulation is now running!${NC}"
echo ""
echo -e "${GREEN}📊 Dashboard URL: http://$SERVER_IP:5000${NC}"
echo ""
echo "📡 Access Points:"
echo "   • TestNet (wlan0) - Channel 1"
echo "   • TestNet (wlan1) - Channel 6"
echo ""
echo "🔄 Roaming Client: wlan2 (Network: 192.168.1.0/24)"
echo ""
echo "📋 Useful Commands:"
echo "   • Check connection: wpa_cli -i wlan2 status"
echo "   • Check signal: iw wlan2 link"
echo "   • View logs: tail -f /home/koti/roaming.log"
echo "   • Dashboard log: tail -f /home/koti/dashboard/dashboard.log"
echo ""
echo "🧪 Test Roaming:"
echo "   • Weaken AP1: iw dev wlan0 set txpower fixed 300"
echo "   • Weaken AP2: iw dev wlan1 set txpower fixed 300"
echo "   • Stop AP1: pkill -f 'hostapd.*ap1.conf'"
echo ""

# Test dashboard connectivity
print_status "Testing dashboard..."
sleep 3
if curl -s -m 5 http://localhost:5000 >/dev/null; then
    echo -e "${GREEN}✅ Dashboard is responding at http://$SERVER_IP:5000${NC}"
else
    echo -e "${YELLOW}⚠️ Dashboard may need more time to start${NC}"
    echo "   Try: tail -f /home/koti/dashboard/dashboard.log"
fi

echo ""
echo -e "${BLUE}🌐 Happy roaming!${NC}"