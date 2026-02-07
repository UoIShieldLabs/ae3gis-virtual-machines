# Manual QEMU Commands Reference

This document provides reference commands for manual VM operations. For normal usage, use `spawn_in_terminals.sh` instead.

---

## Prerequisites

Install QEMU and dependencies:

```bash
brew install qemu
```

---

## Creating the Base Image

### Download Ubuntu Cloud Image

```bash
cd base/
curl -LO https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-arm64.img
mv jammy-server-cloudimg-arm64.img root.qcow2
qemu-img resize root.qcow2 40G
```

### Create UEFI Variable Store

```bash
qemu-img create -f raw vars.fd 64M
```

### Build Cloud-Init Seed ISO

```bash
cd base/
hdiutil makehybrid -iso -joliet -default-volume-name cidata -o seed-init.iso seed-init
```

---

## Running the Base VM

Boot the base image for initial configuration:

```bash
cd base/

sudo qemu-system-aarch64 \
  -accel hvf -machine virt,highmem=on -cpu host \
  -smp 4 -m 8192 \
  -bios /opt/homebrew/share/qemu/edk2-aarch64-code.fd \
  -drive if=pflash,format=raw,unit=1,file=vars.fd \
  -drive if=virtio,file=root.qcow2,format=qcow2 \
  -drive if=virtio,file=seed-init.iso,format=raw,readonly=on \
  -nic vmnet-bridged,ifname=en1,model=virtio-net-pci \
  -nographic
```

---

## Installing Docker (Inside VM)

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl

sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

sudo systemctl enable --now docker
sudo usermod -aG docker gns3
```

---

## GNS3 Server Configuration

### Config File

Located at `/home/gns3/.config/GNS3/gns3_server.conf`:

```ini
[Server]
host = 0.0.0.0
port = 3080
auth = True
user = gns3
password = gns3
projects_path = /home/gns3/projects
console_start_port = 5000
console_end_port = 6999
```

### Firewall Rules

```bash
sudo ufw allow 3080/tcp
sudo ufw allow 5000:6999/tcp
sudo ufw enable
```

---

## Preparing Base Image for Cloning

Before shutting down the base image for cloning, reset the machine ID so each clone gets a unique identity:

```bash
sudo truncate -s 0 /etc/machine-id
sudo rm -f /var/lib/dbus/machine-id
sudo ln -s /etc/machine-id /var/lib/dbus/machine-id
sudo poweroff
```

---

## Creating Overlay Disks Manually

```bash
cd overlays/
qemu-img create -f qcow2 -F qcow2 -b ../base/root.qcow2 overlay-1.qcow2
```

---

## Running an Overlay VM Manually

```bash
sudo qemu-system-aarch64 \
  -accel hvf -machine virt,highmem=on -cpu host \
  -smp 4 -m 12288 \
  -bios /opt/homebrew/share/qemu/edk2-aarch64-code.fd \
  -drive if=pflash,format=raw,unit=1,file=../base/vars.fd \
  -drive if=virtio,file=overlay-1.qcow2,format=qcow2,cache=none,discard=unmap \
  -drive if=virtio,file=../seeds/seed-1.iso,format=raw,readonly=on \
  -nic vmnet-bridged,ifname=en1,model=virtio-net-pci \
  -nographic
```
