#!/usr/bin/env bash
set -euo pipefail

### =======================
### Linux VM Spawner for GNS3
### =======================

COUNT=${1:-3}
START_IP=${2:-10.193.80.101}
GATEWAY_PARAM=${3:-}
PREFIX_LEN=${4:-24}
EXTRA_DNS_CSV=${5:-}

NAME_PREFIX=${6:-overlay}
BRIDGE=${7:-virbr0}

# Paths (assume script in repo root)
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_QCOW2="${BASE_QCOW2:-${ROOT_DIR}/base/root.qcow2}"
VARS_FD="${VARS_FD:-${ROOT_DIR}/base/vars.fd}"

# Auto-detect UEFI firmware location
detect_bios() {
  local paths=(
    "/usr/share/AAVMF/AAVMF_CODE.fd"
    "/usr/share/edk2/aarch64/QEMU_EFI.fd"
    "/usr/share/qemu-efi-aarch64/QEMU_EFI.fd"
    "/usr/share/edk2-ovmf/aarch64/QEMU_CODE.fd"
  )
  for p in "${paths[@]}"; do
    [[ -f "$p" ]] && echo "$p" && return 0
  done
  echo ""
}

BIOS_FD="${BIOS_FD:-$(detect_bios)}"

# VM resources
SMP=${SMP:-4}
MEM_MB=${MEM_MB:-12288}

# Guest interface name
IFACE=${IFACE:-enp0s1}

# DNS defaults
DNS_CSV="${DNS_CSV:-8.8.8.8,1.1.1.1}"

# Derived dirs
OVERLAYS_DIR="${ROOT_DIR}/overlays"
SEEDS_DIR="${ROOT_DIR}/seeds"
mkdir -p "$OVERLAYS_DIR" "$SEEDS_DIR"

### =======================
### Helpers
### =======================
ip2int(){ local IFS=.; read -r a b c d <<<"$1"; echo $(( (a<<24)+(b<<16)+(c<<8)+d )); }
int2ip(){ printf "%d.%d.%d.%d" $(( ($1>>24)&255 )) $(( ($1>>16)&255 )) $(( ($1>>8)&255 )) $(( $1&255 )); }
have(){ command -v "$1" >/dev/null 2>&1; }
die(){ echo "[-] $*" >&2; exit 1; }

detect_host_gateway() {
  if have ip; then
    local gw
    gw=$(ip route show default 2>/dev/null | awk '/default/{print $3}' | head -n1 || true)
    [[ -n "$gw" ]] && echo "$gw" && return 0
  fi
  echo ""
}

# Detect terminal emulator
detect_terminal() {
  if have gnome-terminal; then
    echo "gnome-terminal"
  elif have konsole; then
    echo "konsole"
  elif have xfce4-terminal; then
    echo "xfce4-terminal"
  elif have xterm; then
    echo "xterm"
  else
    echo ""
  fi
}

### Checks
[[ -f "$BASE_QCOW2" ]] || die "Base qcow2 not found: $BASE_QCOW2"
[[ -f "$VARS_FD"   ]] || die "vars.fd not found: $VARS_FD"
[[ -n "$BIOS_FD" && -f "$BIOS_FD" ]] || die "UEFI firmware not found. Set BIOS_FD=/path/to/QEMU_EFI.fd"

TERM_EMU="${TERM_EMU:-$(detect_terminal)}"
[[ -n "$TERM_EMU" ]] || die "No terminal emulator found. Install gnome-terminal, konsole, or xterm."

echo "[*] Spawning ${COUNT} VM(s) starting at ${START_IP} (${NAME_PREFIX}-1..${NAME_PREFIX}-${COUNT})"
echo "    Bridge: ${BRIDGE} | Base: ${BASE_QCOW2} | RAM: ${MEM_MB} | vCPU: ${SMP}"
echo "    Terminal: ${TERM_EMU} | BIOS: ${BIOS_FD}"

# Warm up sudo
echo "[*] Warming up sudo..."
sudo -v

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

  # Gateway
  GW="$GATEWAY_PARAM"
  if [[ -z "$GW" ]]; then
    GW="$(detect_host_gateway)"
    if [[ -z "$GW" ]]; then
      echo "[!] Could not auto-detect gateway"
    fi
  fi

  # DNS
  DNS_COMBINED="$DNS_CSV"
  if [[ -n "$EXTRA_DNS_CSV" ]]; then
    DNS_COMBINED="${DNS_COMBINED},${EXTRA_DNS_CSV}"
  fi

  mkdir -p "$SEED_INIT"

  # 1) Overlay disk
  if [[ ! -f "$OVL" ]]; then
    echo "[*] Creating overlay: $OVL"
    qemu-img create -f qcow2 -F qcow2 -b "$BASE_QCOW2" "$OVL" >/dev/null
  else
    echo "[=] Overlay exists, skipping: $OVL"
  fi

  # 1b) Per-VM vars.fd
  if [[ ! -f "$VM_VARS" ]]; then
    echo "[*] Creating VM-specific vars.fd: $VM_VARS"
    cp "$VARS_FD" "$VM_VARS"
  else
    echo "[=] VM vars.fd exists, skipping: $VM_VARS"
  fi

  # 2) Cloud-init files
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

  cat >"${SEED_INIT}/meta-data" <<EOF
instance-id: ${NAME}
local-hostname: ${NAME}
EOF

  cat >"${SEED_INIT}/99-disable-network-config.cfg" <<'EOF'
network:
  config: disabled
EOF

  cat >"${SEED_INIT}/99-cloud-config.cfg" <<'EOF'
cloud_final_modules:
 - [scripts-per-once, always]

network:
  config: disabled
EOF

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
  - sudo rm -f /etc/netplan/50-cloud-init.yaml
  - sudo netplan apply
  - sudo rm -f /etc/machine-id
  - sudo rm -f /var/lib/dbus/machine-id
  - sudo systemd-machine-id-setup
  - sudo reboot

runcmd:
  - 'su - gns3 -c "mkdir -p /home/gns3/projects /home/gns3/.config/GNS3"'
  - 'git clone https://github.com/GNS3/gns3-server.git /opt/gns3-server || true'
  - 'chown -R gns3:gns3 /opt/gns3-server'
  - 'su - gns3 -c "python3 -m pip install --user -r /opt/gns3-server/requirements.txt"'
  - 'su - gns3 -c "cd /opt/gns3-server && python3 setup.py install --user"'
  - 'cp /opt/gns3-server/resources/systemd/gns3.service /etc/systemd/system/gns3server.service'
  - 'systemctl daemon-reload'
  - 'systemctl enable --now gns3server.service'
  - 'ufw allow 3080/tcp || true'
  - 'yes | ufw enable || true'
EOF

  # 3) Build seed ISO
  echo "[*] Building seed ISO: ${SEED_ISO}"
  if have genisoimage; then
    genisoimage -output "$SEED_ISO" -volid cidata -joliet -rock "$SEED_INIT" 2>/dev/null
  elif have mkisofs; then
    mkisofs -output "$SEED_ISO" -volid cidata -joliet -rock "$SEED_INIT" 2>/dev/null
  elif have xorrisofs; then
    xorrisofs -o "$SEED_ISO" -V cidata -J -R "$SEED_INIT" 2>/dev/null
  else
    die "No ISO builder found (need genisoimage, mkisofs, or xorrisofs)."
  fi

  MAC_SUFFIX=$(printf "%02x" $((50 + i)))
  UNIQUE_UUID=$(uuidgen)

  # 4) Build QEMU command
  QEMU_CMD="sudo qemu-system-aarch64 \
    -enable-kvm -machine virt,highmem=on -cpu host \
    -smp ${SMP} -m ${MEM_MB} \
    -bios ${BIOS_FD} \
    -drive if=pflash,format=raw,unit=1,file=${VM_VARS} \
    -drive if=virtio,file=${OVL},format=qcow2,cache=none,discard=unmap \
    -drive if=virtio,file=${SEED_ISO},format=raw,readonly=on \
    -netdev bridge,id=net0,br=${BRIDGE} \
    -device virtio-net-pci,netdev=net0,mac=52:54:00:12:34:${MAC_SUFFIX} \
    -uuid ${UNIQUE_UUID} \
    -name ${NAME} \
    -nographic"

  # 5) Open terminal and run
  case "$TERM_EMU" in
    gnome-terminal)
      gnome-terminal -- bash -c "cd ${ROOT_DIR} && ${QEMU_CMD}; exec bash"
      ;;
    konsole)
      konsole -e bash -c "cd ${ROOT_DIR} && ${QEMU_CMD}; exec bash" &
      ;;
    xfce4-terminal)
      xfce4-terminal -e "bash -c 'cd ${ROOT_DIR} && ${QEMU_CMD}; exec bash'" &
      ;;
    xterm)
      xterm -e "cd ${ROOT_DIR} && ${QEMU_CMD}; exec bash" &
      ;;
  esac

  echo "[+] Launched ${NAME} (IP ${IP}) in a new terminal"
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
