#!/usr/bin/env python3
import argparse, ipaddress, os, shutil, subprocess, sys, textwrap, csv
from pathlib import Path

# ---------- Defaults (tweak if you like) ----------
DEFAULT_COUNT        = 3
DEFAULT_START_IP     = "10.193.80.101"
DEFAULT_NAME_PREFIX  = "overlay"
DEFAULT_BRIDGE       = "en1"  # macOS bridged interface (Wi-Fi is often en0/en1)
DEFAULT_BASE_QCOW2   = "base/root.qcow2"
DEFAULT_VARS_FD      = "base/vars.fd"
DEFAULT_BIOS         = "/opt/homebrew/share/qemu/edk2-aarch64-code.fd"
DEFAULT_SMP          = 4
DEFAULT_MEM_MB       = 4096
DEFAULT_IFACE_NAME   = "enp0s1"      # guest netplan interface name
DEFAULT_PREFIX_LEN   = 24
DEFAULT_GATEWAY      = "10.193.80.64"        # auto: x.y.z.1 from start-ip’s /24
DEFAULT_DNS          = "10.193.80.64,8.8.8.8"
DEFAULT_WORKDIR      = "."         # repo root (expects ./overlays and ./seeds)
DEFAULT_DAEMONIZE    = True
DEFAULT_SHOW_QEMU    = False       # if True, runs in foreground (-nographic, no -daemonize)

# ---------- Templates (kept minimal and robust) ----------
USER_DATA_TPL = """#cloud-config
preserve_hostname: false
hostname: {hostname}
fqdn: {hostname}.local

ssh_pwauth: true
users:
  - name: gns3
    groups: [sudo]
    shell: /bin/bash
    sudo: 'ALL=(ALL) NOPASSWD:ALL'
    lock_passwd: false
    # You can add your key later if you want:
    # ssh_authorized_keys: ["ssh-ed25519 AAAA... your_key_here"]

chpasswd:
  list: |
    gns3:gns3
  expire: false

package_update: true
packages:
  - openssh-server
  - python3
  - python3-pip
  - git
  - ufw
  - qemu-guest-agent

write_files:
  - path: /etc/ssh/sshd_config.d/99-cloud-ssh.conf
    owner: root:root
    permissions: '0644'
    content: |
      PasswordAuthentication yes
      PubkeyAuthentication yes
      PermitRootLogin no
      KbdInteractiveAuthentication no
      UsePAM yes

runcmd:
  - systemctl enable --now qemu-guest-agent || true
  - systemctl enable --now ssh || systemctl enable --now sshd || true
  - ufw allow OpenSSH || ufw allow 22/tcp
  - yes | ufw enable || true
  - 'echo "Static IP configured for {iface} at {ip}/{prefix} (gw {gateway})"'
"""

NETWORK_CONFIG_TPL = """version: 2
ethernets:
  {iface}:
    dhcp4: no
    addresses:
      - {ip}/{prefix}
    gateway4: {gateway}
    nameservers:
      addresses: [{dns_list}]
"""

META_DATA_TPL = """instance-id: {hostname}
local-hostname: {hostname}
"""

DISABLE_NET_CFG = """# Prevent cloud-init from trying to (re)manage network after we set netplan
network: {config: disabled}
"""

CLOUD_CFG_EXTRA = """# Extra cloud-init config placeholder (keep if you want to add future toggles)
cloud_final_modules:
 - [scripts-per-once, always]

network:
  config: disabled
"""

# --------------------------------------------------------

def ip_next(ip_str, step):
    ip = ipaddress.ip_address(ip_str)
    return str(ip + step)

def guess_gateway(start_ip, prefix_len):
    # Simple heuristic: x.y.z.1 for /24; otherwise network + 1
    net = ipaddress.ip_network(f"{start_ip}/{prefix_len}", strict=False)
    return str(list(net.hosts())[0])

def ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)

def run(cmd, check=True, capture=False, env=None):
    try:
        if capture:
            return subprocess.run(cmd, check=check, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        else:
            return subprocess.run(cmd, check=check, env=env)
    except subprocess.CalledProcessError as e:
        print("\n[!] Command failed:", " ".join(cmd))
        if e.stdout:
            print("--- stdout ---\n", e.stdout)
        if e.stderr:
            print("--- stderr ---\n", e.stderr)
        raise


def build_seed_iso(seed_init_dir: Path, out_iso: Path):
    # 1) Try hdiutil with fork-safety disabled (prevents NSNumber crash)
    tmp_out = out_iso.with_suffix("")  # hdiutil writes tmp_out + ".iso"
    env = os.environ.copy()
    env["OBJC_DISABLE_INITIALIZE_FORK_SAFETY"] = "YES"

    try:
        if tmp_out.exists():
            if tmp_out.is_dir():
                shutil.rmtree(tmp_out)
            else:
                tmp_out.unlink()
        print(f"Creating hybrid image via hdiutil -> {out_iso.name}")
        run(["hdiutil", "makehybrid", "-iso", "-joliet", "-default-volume-name", "cidata",
             "-o", str(tmp_out), str(seed_init_dir)], env=env)
        produced = tmp_out.with_suffix(".iso")
        if produced != out_iso:
            if out_iso.exists():
                out_iso.unlink()
            produced.rename(out_iso)
        return
    except Exception as _:
        print("[*] hdiutil failed; trying mkisofs/xorrisofs fallback...")

    # 2) Try mkisofs (cdrtools)
    if shutil.which("mkisofs"):
        run(["mkisofs", "-output", str(out_iso), "-volid", "cidata", "-joliet", "-rock", str(seed_init_dir)])
        return

    # 3) Try xorrisofs
    if shutil.which("xorrisofs"):
        run(["xorrisofs", "-o", str(out_iso), "-V", "cidata", "-J", "-R", str(seed_init_dir)])
        return

    raise RuntimeError("No ISO builder succeeded (hdiutil/mkisofs/xorrisofs). Install one and retry.")


def main():
    ap = argparse.ArgumentParser(
        description="Create and launch QEMU aarch64 overlay VMs with sequential static IPs (macOS).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    ap.add_argument("--count", type=int, default=DEFAULT_COUNT, help="Number of VMs to create")
    ap.add_argument("--start-ip", default=DEFAULT_START_IP, help="First static IP to assign")
    ap.add_argument("--name-prefix", default=DEFAULT_NAME_PREFIX, help="VM name prefix (overlay-1, overlay-2, ...)")
    ap.add_argument("--bridge", default=DEFAULT_BRIDGE, help="macOS bridged interface (en0/en1/…)")
    ap.add_argument("--base-qcow2", default=DEFAULT_BASE_QCOW2, help="Path to base/root.qcow2")
    ap.add_argument("--vars", default=DEFAULT_VARS_FD, help="Path to base/vars.fd")
    ap.add_argument("--bios", default=DEFAULT_BIOS, help="Path to edk2-aarch64-code.fd")
    ap.add_argument("--smp", type=int, default=DEFAULT_SMP, help="vCPU count per VM")
    ap.add_argument("--mem", type=int, default=DEFAULT_MEM_MB, help="RAM (MiB) per VM")
    ap.add_argument("--iface", default=DEFAULT_IFACE_NAME, help="Guest interface name used in netplan")
    ap.add_argument("--prefix", type=int, default=DEFAULT_PREFIX_LEN, help="CIDR prefix length")
    ap.add_argument("--gateway", default=DEFAULT_GATEWAY, help="Gateway IP (auto if omitted)")
    ap.add_argument("--dns", default=DEFAULT_DNS, help="Comma-separated DNS list")
    ap.add_argument("--workdir", default=DEFAULT_WORKDIR, help="Working directory (expects ./overlays and ./seeds)")
    ap.add_argument("--no-daemonize", action="store_true", help="Run QEMU in foreground (no -daemonize)")
    ap.add_argument("--dry-run", action="store_true", help="Show actions without executing")
    args = ap.parse_args()

    workdir = Path(args.workdir).resolve()
    overlays_dir = workdir / "overlays"
    seeds_dir    = workdir / "seeds"

    ensure_dir(overlays_dir)
    ensure_dir(seeds_dir)

    base_qcow2 = (workdir / args.base_qcow2).resolve()
    vars_fd    = (workdir / args.vars).resolve()
    bios_fd    = Path(args.bios).resolve()

    if shutil.which("ifconfig"):
        print("\n[*] Host interfaces (for reference):")
        run(["ifconfig"], check=False)
    if not base_qcow2.exists():
        sys.exit(f"[!] Base qcow not found: {base_qcow2}")
    if not vars_fd.exists():
        sys.exit(f"[!] vars.fd not found: {vars_fd}")
    if not bios_fd.exists():
        sys.exit(f"[!] BIOS fd not found: {bios_fd}")

    # Gateway default
    gateway = args.gateway or guess_gateway(args.start_ip, args.prefix)
    dns_list = ",".join([d.strip() for d in args.dns.split(",") if d.strip()])

    rows = []
    curr_ip = ipaddress.ip_address(args.start_ip)

    print(f"[*] Creating {args.count} VM(s) starting at {args.start_ip} ({args.name_prefix}-1..{args.name_prefix}-{args.count})")
    print(f"    Bridge: {args.bridge}  |  Base: {base_qcow2}  |  RAM: {args.mem}  |  vCPU: {args.smp}")

    for i in range(1, args.count + 1):
        name = f"{args.name_prefix}-{i}"
        ip   = str(curr_ip)
        curr_ip += 1

        ovl_disk   = overlays_dir / f"root-{i}.qcow2"
        seed_init  = seeds_dir / f"seed-init-{i}"
        seed_iso   = seeds_dir / f"seed-{i}.iso"
        pidfile    = workdir / f"{name}.pid"

        # 1) Create overlay qcow2
        if args.dry_run:
            print(f"DRYRUN: qemu-img create -f qcow2 -F qcow2 -b {base_qcow2} {ovl_disk}")
        else:
            if ovl_disk.exists():
                print(f"[!] Overlay exists, skipping create: {ovl_disk}")
            else:
                run(["qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", str(base_qcow2), str(ovl_disk)])
        
        # 2) Render seed-init files
        ensure_dir(seed_init)
        user_data = USER_DATA_TPL.format(hostname=name, iface=args.iface, ip=ip, prefix=args.prefix, gateway=gateway)
        net_cfg   = NETWORK_CONFIG_TPL.format(iface=args.iface, ip=ip, prefix=args.prefix, gateway=gateway, dns_list=dns_list)
        meta_data = META_DATA_TPL.format(hostname=name)

        files = {
            seed_init / "user-data": user_data,
            seed_init / "network-config": net_cfg,
            seed_init / "meta-data": meta_data,
            seed_init / "99-disable-network-config.cfg": DISABLE_NET_CFG,
            seed_init / "99-cloud-config.cfg": CLOUD_CFG_EXTRA,
        }
        if args.dry_run:
            print(f"DRYRUN: write seed-init-{i} (user-data, network-config, meta-data, cfgs)")
        else:
            for path, content in files.items():
                path.write_text(content)

        # 3) Build seed iso
        if args.dry_run:
            print(f"DRYRUN: hdiutil makehybrid -iso -joliet -default-volume-name cidata -o {seed_iso.with_suffix('')} {seed_init}")
        else:
            build_seed_iso(seed_init, seed_iso)

        # 4) Launch QEMU
        qemu_cmd = [
            "sudo", "qemu-system-aarch64",
            "-accel", "hvf", "-machine", "virt,highmem=on", "-cpu", "host",
            "-smp", str(args.smp), "-m", str(args.mem),
            "-bios", str(bios_fd),
            "-drive", f"if=pflash,format=raw,unit=1,file={vars_fd}",
            "-drive", f"if=virtio,file={ovl_disk},format=qcow2,cache=none,discard=unmap",
            "-drive", f"if=virtio,file={seed_iso},format=raw,readonly=on",
            "-nic", f"vmnet-bridged,ifname={args.bridge},model=virtio-net-pci",
            "-name", name,
        ]
        if args.no_daemonize:
            qemu_cmd += ["-nographic"]
        else:
            qemu_cmd += ["-daemonize", "-pidfile", str(pidfile), "-serial", "null", "-monitor", "none"]

        if args.dry_run:
            print("DRYRUN:", " ".join(qemu_cmd))
            pid_value = ""
        else:
            if not seed_iso.exists():
                raise FileNotFoundError(f"Seed ISO missing: {seed_iso}")
            # Optional: quick identification
            try:
                if shutil.which("hdiutil"):
                    run(["hdiutil", "imageinfo", str(seed_iso)], check=False)
            except Exception:
                pass

            run(qemu_cmd)
            pid_value = ""
            if pidfile.exists():
                try:
                    pid_value = pidfile.read_text().strip()
                except Exception:
                    pass

        rows.append({"NAME": name, "IP": ip, "DISK": str(ovl_disk), "SEED_ISO": str(seed_iso), "PID": pid_value})

    # 5) Write instances.csv and print summary
    csv_path = workdir / "instances.csv"
    if args.dry_run:
        print(f"DRYRUN: would write {csv_path}")
    else:
        with csv_path.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["NAME","IP","DISK","SEED_ISO","PID"])
            w.writeheader()
            for r in rows: w.writerow(r)

    # Summary
    print("\nSummary")
    print("-------")
    for r in rows:
        print(f"{r['NAME']:>12}  {r['IP']:>15}   pid={r['PID'] or '-'}")
    print(f"\nInstances file: {csv_path}")

if __name__ == "__main__":
    main()
