#!/usr/bin/env bash
set -euo pipefail

### =======================
### Defaults (override via args)
### =======================
COUNT=${1:-3}
START_IP=${2:-10.193.80.101}
GATEWAY_PARAM=${3:-}          # if set, use as gateway; else auto-detect from host default route
PREFIX_LEN=${4:-24}           # CIDR
EXTRA_DNS_CSV=${5:-}          # extra DNS (comma-separated) appended to defaults

NAME_PREFIX=${6:-overlay}
BRIDGE=${7:-en1}

# Optional positional overrides:

# Paths (assume script in repo root)
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_QCOW2="${BASE_QCOW2:-${ROOT_DIR}/base/root.qcow2}"
VARS_FD="${VARS_FD:-${ROOT_DIR}/base/vars.fd}"
BIOS_FD="${BIOS_FD:-/opt/homebrew/share/qemu/edk2-aarch64-code.fd}"

# VM resources
SMP=${SMP:-4}
MEM_MB=${MEM_MB:-12288}

# Guest iface name used by your original config
IFACE=${IFACE:-enp0s1}


# DNS base defaults (always included)
DNS_CSV="${DNS_CSV:-8.8.8.8,1.1.1.1}"

# Derived dirs
OVERLAYS_DIR="${ROOT_DIR}/overlays"
SEEDS_DIR="${ROOT_DIR}/seeds"
mkdir -p "$OVERLAYS_DIR" "$SEEDS_DIR"

### =======================
### helpers
### =======================
ip2int(){ local IFS=.; read -r a b c d <<<"$1"; echo $(( (a<<24)+(b<<16)+(c<<8)+d )); }
int2ip(){ printf "%d.%d.%d.%d" $(( ($1>>24)&255 )) $(( ($1>>16)&255 )) $(( ($1>>8)&255 )) $(( $1&255 )); }
have(){ command -v "$1" >/dev/null 2>&1; }
die(){ echo "[-] $*" >&2; exit 1; }

# Auto-detect host default gateway (macOS) as a safe, non-.1 default
detect_host_gateway() {
  if have route; then
    local gw
    gw=$(route -n get default 2>/dev/null | awk '/gateway:/{print $2}' | head -n1 || true)
    [[ -n "$gw" ]] && echo "$gw" && return 0
  fi
  # Fallback (only if detection fails): keep empty to avoid bad assumptions
  echo ""
}

### checks
[[ -f "$BASE_QCOW2" ]] || die "Base qcow2 not found: $BASE_QCOW2"
[[ -f "$VARS_FD"   ]]  || die "vars.fd not found: $VARS_FD"
[[ -f "$BIOS_FD"   ]]  || die "BIOS fd not found: $BIOS_FD"

echo "[*] Spawning ${COUNT} VM(s) starting at ${START_IP} (${NAME_PREFIX}-1..${NAME_PREFIX}-${COUNT})"
echo "    Bridge: ${BRIDGE} | Base: ${BASE_QCOW2} | RAM: ${MEM_MB} | vCPU: ${SMP} | Iface: ${IFACE}"

# warm up sudo (so every Terminal window doesn't prompt)
echo "[*] Warming up sudo…"
sudo -v

# Terminal vs iTerm
TERM_APP="${TERM_APP:-terminal}"
TERM_APP_LOWER="$(echo "$TERM_APP" | tr '[:upper:]' '[:lower:]')"

# IP math init
ip_int=$(ip2int "$START_IP")
SUMMARY=()

for i in $(seq 1 "$COUNT"); do
  NAME="${NAME_PREFIX}-${i}"
  IP=$(int2ip "$ip_int")
  ip_int=$((ip_int+1))

  OVL="${OVERLAYS_DIR}/overlay-${i}.qcow2"
  VM_VARS="${OVERLAYS_DIR}/${NAME}-vars.fd"
  SEED_INIT="${SEEDS_DIR}/seed-init-${i}"
  SEED_ISO="${SEEDS_DIR}/seed-${i}.iso"

  # Gateway: use provided param; else auto-detect from host default route
  GW="$GATEWAY_PARAM"
  if [[ -z "$GW" ]]; then
    GW="$(detect_host_gateway)"
    if [[ -z "$GW" ]]; then
      echo "[!] Could not auto-detect gateway; leaving empty. Your bootcmd will still write file but netplan may need a valid gateway."
    fi
  fi

  # DNS: defaults + optional extras
  DNS_COMBINED="$DNS_CSV"
  if [[ -n "$EXTRA_DNS_CSV" ]]; then
    DNS_COMBINED="${DNS_COMBINED},${EXTRA_DNS_CSV}"
  fi
  DNS_LIST="${DNS_COMBINED//,/,\ }"   # pretty spacing in YAML

  mkdir -p "$SEED_INIT"

  # 1) overlay disk
  if [[ ! -f "$OVL" ]]; then
    echo "[*] Creating overlay: $OVL"
    qemu-img create -f qcow2 -F qcow2 -b "$BASE_QCOW2" "$OVL" >/dev/null
  else
    echo "[=] Overlay exists, skipping: $OVL"
  fi

  # 1b) per-VM vars.fd (UEFI variables must be unique per VM)
  if [[ ! -f "$VM_VARS" ]]; then
    echo "[*] Creating VM-specific vars.fd: $VM_VARS"
    cp "$VARS_FD" "$VM_VARS"
  else
    echo "[=] VM vars.fd exists, skipping: $VM_VARS"
  fi

  # 2) cloud-init files (your original content with variable substitution)

  # network-config (kept minimal—your original uses bootcmd for netplan)
  cat >"${SEED_INIT}/network-config" <<EOF
version: 2
ethernets:
    ${IFACE}:
        dhcp4: false
        addresses:
            - ${IP}/${PREFIX_LEN}
        gateway4: ${GW}
        nameservers:
            addresses:
                - ${GW}
                - 8.8.8.8
EOF

  # meta-data
  cat >"${SEED_INIT}/meta-data" <<EOF
instance-id: ${NAME}
local-hostname: ${NAME}
EOF

  # 99-disable-network-config.cfg
  cat >"${SEED_INIT}/99-disable-network-config.cfg" <<'EOF'
# Prevent cloud-init from managing network if we've already set netplan
network:
  config: disabled
EOF

  # 99-cloud-config.cfg (placeholder for extras if you want later)
  cat >"${SEED_INIT}/99-cloud-config.cfg" <<'EOF'
# Extra cloud-init config (placeholder)
cloud_final_modules:
 - [scripts-per-once, always]

network:
  config: disabled
EOF

  # user-data — EXACT structure from your original, with IP/GW/DNS/IFACE injected
  cat >"${SEED_INIT}/user-data" <<EOF
ssh_pwauth: true

users:
  - name: gns3
    groups: [sudo]
    shell: /bin/bash
    sudo: 'ALL=(ALL) NOPASSWD:ALL'
    lock_passwd: false

chpasswd:
  list: |
    gns3:gns3
  expire: false

package_update: true
packages:
  - python3
  - python3-pip
  - git
  - ufw
  - iproute2
  - net-tools
  - curl

write_files:
  - path: /home/gns3/.config/GNS3/gns3_server.conf
    owner: gns3:gns3
    permissions: '0644'
    content: |
      [Server]
      host = 0.0.0.0
      port = 3080
      auth = True
      user = gns3
      password = gns3
      projects_path = /home/gns3/projects

bootcmd:
  # 1. Create the Netplan configuration file with the static IP
  - echo "network:
      version: 2
      ethernets:
          ${IFACE}:
              dhcp4: false
              addresses:
                  - ${IP}/${PREFIX_LEN}
              gateway4: ${GW}
              nameservers:
                  addresses: [${GW}, 8.8.8.8]" | sudo tee /etc/netplan/01-static-net.yaml > /dev/null

  # 2. Delete the default cloud-init Netplan file (if it exists)
  - sudo rm -f /etc/netplan/50-cloud-init.yaml

  # 3. Apply the new static configuration (must be run early)
  - sudo netplan apply
  - sudo rm -f /etc/machine-id
  - sudo rm -f /var/lib/dbus/machine-id  # Also important for D-Bus
  - sudo systemd-machine-id-setup       # Generates new ID
  - sudo reboot

runcmd:
  # base folders
  - 'su - gns3 -c "mkdir -p /home/gns3/projects /home/gns3/.config/GNS3"'

  # clone official repo
  - 'git clone https://github.com/GNS3/gns3-server.git /opt/gns3-server || true'
  - 'chown -R gns3:gns3 /opt/gns3-server'

  # install python deps + gns3server (to ~/.local for user gns3)
  - 'su - gns3 -c "python3 -m pip install --user -r /opt/gns3-server/requirements.txt"'
  - 'su - gns3 -c "cd /opt/gns3-server && python3 setup.py install --user"'

  # install systemd unit from repo
  - 'cp /opt/gns3-server/resources/systemd/gns3.service /etc/systemd/system/gns3server.service'

  # enable + start service
  - 'systemctl daemon-reload'
  - 'systemctl enable --now gns3server.service'

  # firewall (optional; keep if students connect over a trusted segment/VPN)
  - 'ufw allow 3080/tcp || true'
  - 'yes | ufw enable || true'
EOF

  # 3) build seed ISO (macOS-safe; fallback to mkisofs/xorrisofs if available)
  echo "[*] Building seed ISO: ${SEED_ISO}"
  ISO_TMP="${SEED_ISO%.iso}"
  if have hdiutil; then
    OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES \
    hdiutil makehybrid -iso -joliet -default-volume-name cidata -o "$ISO_TMP" "$SEED_INIT" >/dev/null
    mv -f "${ISO_TMP}.iso" "$SEED_ISO"
  elif have mkisofs; then
    mkisofs -output "$SEED_ISO" -volid cidata -joliet -rock "$SEED_INIT" >/dev/null
  elif have xorrisofs; then
    xorrisofs -o "$SEED_ISO" -V cidata -J -R "$SEED_INIT" >/dev/null
  else
    die "No ISO builder found (need hdiutil or mkisofs or xorrisofs)."
  fi
  
  MAC_SUFFIX=$(printf "%02x" $((50 + i)))
  UNIQUE_UUID=$(uuidgen)
  # 4) open a new Terminal/iTerm window and run QEMU in foreground
  QEMU_CMD="sudo qemu-system-aarch64 \
    -accel hvf -machine virt,highmem=on -cpu host \
    -smp ${SMP} -m ${MEM_MB} \
    -bios ${BIOS_FD} \
    -drive if=pflash,format=raw,unit=1,file=${VM_VARS} \
    -drive if=virtio,file=${OVL},format=qcow2,cache=none,discard=unmap \
    -drive if=virtio,file=${SEED_ISO},format=raw,readonly=on \
    -nic vmnet-bridged,ifname=${BRIDGE},model=virtio-net-pci,mac=52:54:00:12:34:${MAC_SUFFIX} \
    -uuid ${UNIQUE_UUID} \
    -name ${NAME} \
    -nographic"
    
    # -nic user,model=virtio-net-pci,dhcpstart=10.0.2.100,mac=52:54:00:12:34:${MAC_SUFFIX} \
    # -nic vmnet-host,model=virtio-net-pci,mac=52:54:00:12:34:${MAC_SUFFIX} \

  if [[ "$TERM_APP_LOWER" == "iterm" ]]; then
    osascript <<OSA
tell application "iTerm"
  create window with default profile
  tell current session of current window to write text "cd ${ROOT_DIR} && ${QEMU_CMD//\"/\\\"}"
end tell
OSA
  else
    osascript <<OSA
tell application "Terminal"
  activate
  do script "cd ${ROOT_DIR} && ${QEMU_CMD//\"/\\\"}"
end tell
OSA
  fi

  echo "[+] Launched ${NAME} (IP ${IP}) in a new ${TERM_APP_LOWER} window"
  SUMMARY+=("${NAME} ${IP}")
done

echo
echo "Summary"
echo "-------"
for line in "${SUMMARY[@]}"; do
  printf "%-16s %s\n" "${line%% *}" "${line##* }"
done

echo
echo "Tip: check each VM's cloud-init progress with:  sudo tail -f /var/log/cloud-init-output.log"
