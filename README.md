# AE3GIS Virtual Machines

A toolkit for spawning multiple QEMU-based GNS3 virtual machines on macOS (Apple Silicon). Designed for instructors to create a base VM with pre-configured scenarios and distribute clones to students, each with an isolated GNS3 environment.

---

## Overview

This repository provides scripts to:

1. **Create a base VM** — Install GNS3, Docker containers, and configure scenarios
2. **Spawn student VMs** — Clone the base image with unique IPs and isolated storage

Each student receives their own GNS3 server instance with identical configurations, enabling hands-on lab exercises in IT/OT network security.

---

## Prerequisites

### Hardware
- macOS with Apple Silicon (M1/M2/M3/M4)
- Minimum 16GB RAM recommended (each VM uses 12GB by default)

### Software

Install QEMU via Homebrew:

```bash
brew install qemu
```

### Network

The script uses bridged networking (`vmnet-bridged`) which requires:
- A network interface to bridge (default: `en1`)
- Static IP range available on your network for student VMs

---

## Directory Structure

```
.
├── spawn_in_terminals.sh    # Main script to spawn VMs
├── spawn-vm-command.md      # Reference: manual QEMU commands
├── base/
│   ├── root.qcow2           # Base disk image (you create this)
│   ├── vars.fd              # UEFI variable store
│   └── seed-init/           # Cloud-init templates for base image
├── overlays/                # Generated: per-VM overlay disks
└── seeds/                   # Generated: per-VM cloud-init configs
```

---

## Quick Start

### Step 1: Create the Base Image

Download Ubuntu 22.04 ARM64 cloud image:

```bash
cd base/
curl -LO https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-arm64.img
mv jammy-server-cloudimg-arm64.img root.qcow2
qemu-img resize root.qcow2 40G
```

Create the UEFI variable store:

```bash
qemu-img create -f raw vars.fd 64M
```

Build the seed ISO for initial setup:

```bash
hdiutil makehybrid -iso -joliet -default-volume-name cidata -o seed-init.iso seed-init
```

Boot the base VM to install GNS3 and configure your environment:

```bash
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

Once booted, configure your GNS3 projects, install Docker images, and set up any scenarios you need. Then shut down the VM cleanly:

```bash
sudo shutdown -h now
```

### Step 2: Spawn Student VMs

Use the main script to create multiple VM clones:

```bash
./spawn_in_terminals.sh COUNT START_IP [GATEWAY] [PREFIX_LEN]
```

**Example — Spawn 10 VMs starting at IP 10.193.80.101:**

```bash
./spawn_in_terminals.sh 10 10.193.80.101
```

This will:
- Create overlay disks (copy-on-write, minimal storage)
- Generate unique cloud-init configs with static IPs
- Open each VM in a separate Terminal window

### Step 3: Distribute to Students

Each student connects to their assigned VM:

| Student | IP Address | GNS3 Web UI | SSH |
|---------|------------|-------------|-----|
| 1 | 10.193.80.101 | http://10.193.80.101:3080 | `ssh gns3@10.193.80.101` |
| 2 | 10.193.80.102 | http://10.193.80.102:3080 | `ssh gns3@10.193.80.102` |
| ... | ... | ... | ... |

**Default Credentials:** `gns3` / `gns3`

---

## Script Parameters

```bash
./spawn_in_terminals.sh COUNT START_IP [GATEWAY] [PREFIX_LEN] [EXTRA_DNS] [NAME_PREFIX] [BRIDGE]
```

| Parameter | Default | Description |
|-----------|---------|-------------|
| `COUNT` | 3 | Number of VMs to spawn |
| `START_IP` | 10.193.80.101 | First VM's static IP |
| `GATEWAY` | auto-detect | Network gateway (auto-detected from host) |
| `PREFIX_LEN` | 24 | CIDR prefix length |
| `EXTRA_DNS` | — | Additional DNS servers (comma-separated) |
| `NAME_PREFIX` | overlay | VM naming prefix |
| `BRIDGE` | en1 | macOS network interface to bridge |

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SMP` | 4 | vCPUs per VM |
| `MEM_MB` | 12288 | RAM per VM (MB) |
| `BASE_QCOW2` | base/root.qcow2 | Path to base image |
| `TERM_APP` | terminal | Use `iterm` for iTerm2 |

---

## Managing VMs

### View Running VMs

Each VM runs in its own Terminal window. The script prints a summary:

```
Summary
-------
overlay-1        10.193.80.101
overlay-2        10.193.80.102
overlay-3        10.193.80.103
```

### Stop a VM

Close the Terminal window or press `Ctrl+A`, then `X` in the QEMU console.

### Check Cloud-Init Progress

On the VM:

```bash
sudo tail -f /var/log/cloud-init-output.log
```

### Reset a VM

Delete its overlay and seed files, then re-run the spawn script:

```bash
rm overlays/overlay-1.qcow2 overlays/overlay-1-vars.fd seeds/seed-1.iso
rm -rf seeds/seed-init-1
./spawn_in_terminals.sh 1 10.193.80.101
```

---

## Reference

For manual QEMU commands and advanced configuration, see [spawn-vm-command.md](spawn-vm-command.md).

---

## Related

This repository is part of **AE3GIS** (Agile Emulated Educational Environment for Guided Industrial Security Training), a platform for cybersecurity education in ICS/IT-OT environments.

---

## License

See [LICENSE](LICENSE) for details.
