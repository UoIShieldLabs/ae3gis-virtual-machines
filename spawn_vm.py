#!/usr/bin/env python3
"""
Spawn a bridged GNS3 VM overlay on macOS (Apple Silicon) and print its IP.

- Creates an overlay qcow2 on top of base/gold.qcow2
- Launches qemu-system-aarch64 with hvf, bridged NIC on en1 (Wi-Fi)
- Uses cache=none,discard=unmap for safer disk I/O
- Sets a unique, locally-administered MAC on the NIC
- Polls ARP for that MAC; lightly pings the /24 to tickle ARP if needed
- Prints: VM name, PID, MAC, IP, API URL (3080), creds

Requirements (host/macOS):
  - QEMU via Homebrew, with edk2-aarch64-code.fd present
  - Base tree like:
      ~/gns3-qemu-B/
        base/gold.qcow2
        base/vars.fd
        base/edk2-code.fd (optional convenience symlink)
        overlays/
        logs/

Usage:
  python3 spawn_vm.py --name sC \
    [--base-dir ~/gns3-qemu-B] [--ifname en1] [--cpus 4] [--ram-mb 4096] [--timeout 120]
"""

import argparse
import ipaddress
import json
import os
import random
import re
import shlex
import socket
import string
import subprocess
import sys
import time
from pathlib import Path

def run(cmd, check=True, capture=True, sudo=False):
    if isinstance(cmd, str):
        cmd_list = shlex.split(cmd)
    else:
        cmd_list = cmd
    if sudo and os.geteuid() != 0:
        cmd_list = ["sudo"] + cmd_list
    result = subprocess.run(cmd_list,
                            stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.PIPE if capture else None,
                            text=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"CMD failed: {' '.join(cmd_list)}\nSTDERR:\n{result.stderr}")
    return (result.stdout or "").strip()

def ensure_paths(base_dir: Path, name: str):
    base = base_dir / "base"
    overlays = base_dir / "overlays"
    logs = base_dir / "logs"
    for p in (base, overlays, logs):
        p.mkdir(parents=True, exist_ok=True)
    ov = overlays / f"{name}.qcow2"
    pidf = overlays / f"{name}.pid"
    logf = (base_dir / "logs" / f"{name}.log")
    return base, overlays, logs, ov, pidf, logf

def make_overlay(gold: Path, overlay: Path):
    if not gold.exists():
        raise FileNotFoundError(f"Base image not found: {gold}")
    if overlay.exists():
        return  # reuse
    run(["qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", str(gold), str(overlay)])

def gen_mac(last_octet=None):
    # Locally-administered MAC: set bit1 of first octet (0x02)
    # Use qemu-friendly OUI 52:54:00
    mac = [0x52, 0x54, 0x00,
           random.randint(0x00, 0x7f),
           random.randint(0x00, 0xff),
           random.randint(0x00, 0xff) if last_octet is None else last_octet & 0xFF]
    return ":".join(f"{b:02x}" for b in mac)

def get_if_ip_netmask(ifname: str):
    # macOS ipconfig helpers
    ip = run(["ipconfig", "getifaddr", ifname], check=False)
    mask = run(["ipconfig", "getoption", ifname, "subnet_mask"], check=False)
    return (ip.strip() or None), (mask.strip() or None)

def cidr_from_ip_mask(ip, mask):
    try:
        m = ipaddress.IPv4Network(f"{ip}/{mask}", strict=False)
        return m
    except Exception:
        # fallback to /24 on same classful subnet
        parts = ip.split(".")
        net = ".".join(parts[:3] + ["0"]) + "/24"
        return ipaddress.IPv4Network(net, strict=False)

def arp_lookup_by_mac(mac_lower: str):
    out = run(["arp", "-an"], check=False)
    for line in out.splitlines():
        # ? (10.193.80.66) at 52:54:00:a1:b2:01 on en1 ifscope [ethernet]
        m = re.search(r"\((?P<ip>\d+\.\d+\.\d+\.\d+)\).* at (?P<mac>[0-9a-f:]{17}) ", line, re.I)
        if m and m.group("mac").lower() == mac_lower:
            return m.group("ip")
    return None

def light_ping_sweep(ifname: str, network: ipaddress.IPv4Network, limit=64):
    # Ping a subset of the subnet to tickle ARP.
    # We only probe up to `limit` hosts around our own IP to keep it quick.
    host_ip, _ = get_if_ip_netmask(ifname)
    if not host_ip:
        return
    host = ipaddress.IPv4Address(host_ip)
    hosts = list(network.hosts())
    # find host index and ping +/- window
    try:
        idx = hosts.index(host)
    except ValueError:
        idx = len(hosts)//2
    window = max(1, min(limit//2, len(hosts)//2))
    candidates = hosts[max(0, idx-window):min(len(hosts), idx+window)]
    # Fire quick pings with 1 probe each; ignore output
    procs = []
    for ip in candidates:
        procs.append(subprocess.Popen(["ping", "-c", "1", "-W", "100", str(ip)],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    # let them finish briefly
    time.sleep(0.5)
    for p in procs:
        try:
            p.poll()
        except Exception:
            pass

def launch_vm(
    base_dir: Path,
    name: str,
    ifname: str,
    cpus: int,
    ram_mb: int,
    mac: str,
    timeout_boot: int = 120
):
    base, overlays, logs, ov, pidf, logf = ensure_paths(base_dir, name)
    gold = base / "gold.qcow2"
    varsfd = base / "vars.fd"
    codefd = Path("/opt/homebrew/share/qemu/edk2-aarch64-code.fd")
    if not codefd.exists():
        raise FileNotFoundError("UEFI code ROM not found at /opt/homebrew/share/qemu/edk2-aarch64-code.fd")
    if not varsfd.exists():
        raise FileNotFoundError(f"vars.fd not found: {varsfd} (create with: qemu-img create -f raw {varsfd} 64M)")
    make_overlay(gold, ov)

    # qemu command
    cmd = [
        "qemu-system-aarch64",
        "-accel", "hvf",
        "-machine", "virt,highmem=on",
        "-cpu", "host",
        "-smp", str(cpus),
        "-m", str(ram_mb),
        "-bios", str(codefd),
        "-drive", f"if=pflash,format=raw,unit=1,file={varsfd}",
        "-drive", f"if=virtio,file={ov},format=qcow2,cache=none,discard=unmap",
        "-nic", f"vmnet-bridged,ifname={ifname},model=virtio-net-pci,mac={mac}",
        "-nographic",
        "-daemonize", "-pidfile", str(pidf),
        "-D", str(logf),
    ]

    # Launch (needs sudo on mac for vmnet)
    run(cmd, sudo=True)

    # Poll for PID file
    t0 = time.time()
    while time.time() - t0 < 5:
        if pidf.exists():
            break
        time.sleep(0.1)
    if not pidf.exists():
        raise RuntimeError("QEMU did not create a PID file; check logs.")

    return ov, pidf, logf

def main():
    ap = argparse.ArgumentParser(description="Spawn a bridged GNS3 VM overlay and print its IP.")
    ap.add_argument("--base-dir", default=str(Path.home() / "gns3-qemu-B"))
    ap.add_argument("--name", required=True, help="Overlay VM name (e.g., sA)")
    ap.add_argument("--ifname", default="en1", help="macOS interface to bridge (en0 Ethernet, en1 Wi-Fi)")
    ap.add_argument("--cpus", type=int, default=4)
    ap.add_argument("--ram-mb", type=int, default=4096)
    ap.add_argument("--timeout", type=int, default=180, help="Seconds to wait for IP discovery")
    args = ap.parse_args()

    base_dir = Path(args.base_dir).expanduser()
    name = args.name

    # Generate a stable random MAC (or derive from name)
    # You can make it deterministic per name if you prefer:
    random.seed(name)  # deterministic per name
    mac = gen_mac()

    # Launch the VM
    overlay, pidf, logf = launch_vm(base_dir, name, args.ifname, args.cpus, args.ram_mb, mac, args.timeout)

    # Get host interface IP & subnet, build /24 if needed
    host_ip, mask = get_if_ip_netmask(args.ifname)
    if not host_ip:
        print("WARNING: Could not determine host interface IP; ARP probing may be limited.", file=sys.stderr)
    subnet = None
    if host_ip and mask:
        try:
            subnet = cidr_from_ip_mask(host_ip, mask)
        except Exception:
            subnet = None

    mac_lower = mac.lower()
    ip_found = None
    t0 = time.time()

    # Discovery loop: check ARP; occasionally tickle LAN with light ping sweep
    sweep_every = 5.0
    next_sweep = time.time() + 1.0
    while time.time() - t0 < args.timeout:
        ip = arp_lookup_by_mac(mac_lower)
        if ip:
            ip_found = ip
            break
        if subnet and time.time() >= next_sweep:
            light_ping_sweep(args.ifname, subnet, limit=64)
            next_sweep = time.time() + sweep_every
        time.sleep(0.5)

    # Read PID
    pid = None
    try:
        pid = Path(pidf).read_text().strip()
    except Exception:
        pass

    info = {
        "vm": name,
        "overlay": str(overlay),
        "pidfile": str(pidf),
        "logfile": str(logf),
        "pid": pid,
        "mac": mac_lower,
        "ip": ip_found,
        "api_url": f"http://{ip_found}:3080" if ip_found else None,
        "auth": {"user": "gns3", "password": "gns3"},
        "notes": "Consoles are on TCP 5000-5999 (UFW opened). Connect GNS3 GUI to api_url with auth."
    }

    print(json.dumps(info, indent=2))
    if not ip_found:
        print("\nTIP: If IP is null, the VM may still be booting. SSH is enabled; once you know the IP, `ssh gns3@<IP>` (pass: gns3).", file=sys.stderr)
        print(f"Log: {logf}", file=sys.stderr)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
