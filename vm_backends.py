"""The shared VM contract and its QEMU/HVF and VirtualBox implementations."""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import time

if TYPE_CHECKING:
    from snp_vm import Runtime, VMProfile


@dataclass(frozen=True)
class NetworkPolicy:
    listen_host: str
    guest_proxy_url: str
    host_firewall: bool
    port: int = 3128


@dataclass(frozen=True)
class SSHAddress:
    host: str
    port: int = 22


@dataclass(frozen=True)
class VMSettings:
    ram_mb: int
    cpus: int
    disk_mb: int
    headless: bool = False
    baseline: bool = False


class Backend(ABC):
    supports_baseline = False

    def __init__(self, runtime: Runtime, profile: VMProfile):
        self.runtime, self.profile = runtime, profile

    @property
    @abstractmethod
    def network(self) -> NetworkPolicy: ...

    @abstractmethod
    def check_new(self) -> None: ...

    @abstractmethod
    def create(self, settings: VMSettings, image: Path, bootstrap, password: str) -> None: ...

    @abstractmethod
    def state(self) -> str: ...

    @abstractmethod
    def start(self, headless: bool = False) -> None: ...

    @abstractmethod
    def stop(self, force: bool = False) -> None: ...

    @abstractmethod
    def ssh_address(self) -> SSHAddress | None: ...

    @abstractmethod
    def running_names(self) -> list[str]: ...

    @abstractmethod
    def snapshots(self) -> list[str]: ...

    @abstractmethod
    def take_snapshot(self, name: str) -> None:
        """Create/replace an offline snapshot of ALL mutable VM state, including firmware."""

    @abstractmethod
    def restore_snapshot(self, name: str) -> None:
        """Restore ALL mutable VM state, or fail before starting the VM."""


class QemuBackend(Backend):
    def __init__(self, runtime, profile):
        super().__init__(runtime, profile)
        self._created_config = None

    @property
    def root(self):
        return self.runtime.vm_dir

    @property
    def config_path(self):
        return self.root / "qemu.json"

    @property
    def network(self):
        return NetworkPolicy("127.0.0.1", "http://10.0.2.100:3128", False)

    @property
    def control_dir(self):
        digest = hashlib.sha256(str(self.root).encode()).hexdigest()[:16]
        return Path(f"/tmp/snp-{os.getuid()}-{digest}")

    @property
    def monitor(self):
        return self.control_dir / "qmp.sock"

    def tool(self, name):
        path = shutil.which(name)
        if not path and not self.runtime.dry_run:
            raise SystemExit(f"{name} not found: brew install qemu")
        return path or name

    def config(self):
        if not self.config_path.exists():
            raise SystemExit(f"VM {self.runtime.name} not found: run `python3 snp.py setup` first")
        return json.loads(self.config_path.read_text())

    def qmp(self, command):
        info = self.control_dir.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise SystemExit(f"unsafe QMP socket directory: {self.control_dir}; expected an owner-only directory")
        with socket.socket(socket.AF_UNIX) as sock:
            sock.settimeout(5)
            sock.connect(str(self.monitor))
            with sock.makefile("rwb") as stream:
                greeting = stream.readline()
                if not greeting:
                    raise ConnectionError("QEMU closed its monitor")
                if "QMP" not in json.loads(greeting):
                    raise RuntimeError("invalid QMP greeting")
                for request in ("qmp_capabilities", command):
                    message = {"execute": request}
                    stream.write((json.dumps(message) + "\n").encode())
                    stream.flush()
                    while True:
                        line = stream.readline()
                        if not line:
                            raise ConnectionError("QEMU closed its monitor")
                        response = json.loads(line)
                        if "error" in response:
                            raise RuntimeError(response["error"])
                        if "return" in response:
                            break
                return response["return"]

    def active(self):
        try:
            self.qmp("query-status")
            return True
        except OSError:
            return False

    def state(self):
        if self.active():
            return "running"
        return "poweroff" if self.config_path.exists() else "not found"

    def firmware(self):
        share = Path(self.tool(self.profile.vm["binary"])).absolute().parent.parent / "share" / "qemu"
        code, variables = share / self.profile.vm["firmware_code"], share / self.profile.vm["firmware_vars"]
        if not self.runtime.dry_run and not (code.is_file() and variables.is_file()):
            raise SystemExit(f"QEMU ARM UEFI firmware not found in {share}; reinstall the Homebrew qemu package")
        return code, variables

    def command(self, config, headless=False):
        # Isolation is backend policy, not an overridable profile option. Each
        # guest proxy connection gets its own nc stream; there is no general NAT.
        net = ("user,id=egress,restrict=on,ipv6=off,"
               f"hostfwd=tcp:127.0.0.1:{config['ssh_port']}-:22,"
               "guestfwd=tcp:10.0.2.100:3128-cmd:/usr/bin/nc 127.0.0.1 3128")
        esc = lambda path: str(path).replace(",", ",,")
        vm = self.profile.vm
        return [self.tool(vm["binary"]), "-name", self.runtime.name,
                "-machine", f"{vm['machine']},accel={vm['accelerator']}", "-cpu", vm["cpu"],
                "-m", str(config["ram"]), "-smp", str(config["cpus"]), "-nodefaults", "-no-user-config",
                "-smbios", "type=1,serial=ds=nocloud",
                "-drive", f"if=pflash,format=raw,readonly=on,file={esc(config['firmware'])}",
                "-drive", f"if=pflash,format=raw,file={esc(self.root / 'vars.fd')}",
                "-drive", f"if=none,id=os,format=qcow2,file={esc(self.root / 'disk.qcow2')}",
                "-device", "virtio-blk-pci,drive=os", "-netdev", net,
                "-device", "virtio-net-pci,netdev=egress,mac=52:54:00:53:4e:50", "-device", "virtio-rng-pci",
                "-qmp", f"unix:{self.monitor},server=on,wait=off",
                "-chardev", f"socket,id=console,path={self.control_dir / 'console.sock'},server=on,wait=off,logfile={esc(self.root / 'serial.log')}",
                "-serial", "chardev:console", "-device", vm["graphics_device"], "-device", "qemu-xhci",
                "-device", "usb-kbd", "-device", "usb-tablet", "-display", "none" if headless else vm["display"],
                "-drive", f"if=none,id=seed,format=raw,readonly=on,file={esc(self.root / 'seed.iso')}",
                "-device", "virtio-blk-pci,drive=seed"]

    def check_new(self):
        # A failed preflight may have left only shared credentials. Reuse those,
        # while refusing any existing disk, seed, metadata or unknown VM state.
        credentials = {"id_ed25519", "id_ed25519.pub", "password", "known_hosts"}
        if self.root.exists() and any(path.name not in credentials for path in self.root.iterdir()):
            raise SystemExit(f"VM state already exists at {self.root}; use --name for another VM, or provision to resume")
        self.firmware()
        self.tool("qemu-img")

    def write_seed(self, bootstrap, network):
        directory, target = self.root / "seed", self.root / "seed.iso"
        if not self.runtime.dry_run:
            directory.mkdir(mode=0o700)
            for name, content in bootstrap.seed_files(network).items():
                (directory / name).write_text(content)
        try:
            self.runtime.run(["hdiutil", "makehybrid", "-iso", "-joliet", "-default-volume-name", "cidata",
                              "-o", target, directory])
            if not self.runtime.dry_run:
                target.chmod(0o600)
        finally:
            if not self.runtime.dry_run:
                shutil.rmtree(directory)

    def create(self, settings, image, bootstrap, password):
        if self.config_path.exists() or (self.root / "disk.qcow2").exists():
            raise SystemExit(f"VM state already exists at {self.root}")
        _, variables = self.firmware()
        if not self.runtime.dry_run:
            info = json.loads(self.runtime.run([self.tool("qemu-img"), "info", "--output=json", image]))
            if info["format"] != "qcow2" or info.get("backing-filename") or info.get("format-specific", {}).get("data", {}).get("data-file"):
                raise SystemExit("--image must be a standalone ARM64 Ubuntu cloud image in qcow2 format")
            shutil.copyfile(image, self.root / "disk.qcow2")
            shutil.copyfile(variables, self.root / "vars.fd")
        else:
            print("+ copy", image, "to", self.root / "disk.qcow2")
        self.runtime.run([self.tool("qemu-img"), "resize", self.root / "disk.qcow2", f"{settings.disk_mb}M"])
        self.write_seed(bootstrap, {"version": 2, "ethernets": {"sandbox": {
            "match": {"macaddress": "52:54:00:53:4e:50"}, "dhcp4": False, "dhcp6": False,
            "accept-ra": False, "optional": True, "addresses": ["10.0.2.15/24"],
            "routes": [{"to": "default", "via": "10.0.2.2"}]}}})
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        self._created_config = {"ram": settings.ram_mb, "cpus": settings.cpus,
                                "ssh_port": port}
        if not self.runtime.dry_run:
            self.config_path.write_text(json.dumps(self._created_config, indent=2) + "\n")

    def start(self, headless=False):
        if self.active():
            return
        config = self.config() if self.config_path.exists() else self._created_config or {
            "ram": self.profile.resources["ram_mb"], "cpus": self.profile.resources["cpus"], "ssh_port": "<allocated port>"}
        config["firmware"] = str(self.firmware()[0])
        command = self.command(config, headless)
        if self.runtime.dry_run:
            self.runtime.run(command)
            return
        if not self.config_path.exists():
            raise SystemExit(f"VM {self.runtime.name} not found")
        self.control_dir.mkdir(mode=0o700, exist_ok=True)
        info = self.control_dir.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise SystemExit(f"unsafe QMP socket directory: {self.control_dir}")
        for name in ("qmp.sock", "console.sock"):
            (self.control_dir / name).unlink(missing_ok=True)
        with (self.root / "qemu.log").open("a") as log:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                       start_new_session=True)
        for _ in range(100):
            if process.poll() is not None:
                raise SystemExit(f"QEMU exited ({process.returncode}); check {self.root / 'qemu.log'}")
            if self.active():
                return
            time.sleep(0.1)
        raise SystemExit(f"QEMU monitor did not start; check {self.root / 'qemu.log'}")

    def stop(self, force=False):
        if self.runtime.dry_run:
            print("+ QMP", "quit" if force else "system_powerdown (then quit if ignored)")
            return
        if not self.active():
            return
        if not force:
            print("waiting for the VM to power off ...", flush=True)
            self.qmp("system_powerdown")
            for _ in range(60):
                if not self.active():
                    return
                time.sleep(1)
        self.qmp("quit")
        self.runtime.wait("QEMU to exit", lambda: not self.active(), 1)

    def ssh_address(self):
        if not self.config_path.exists():
            return SSHAddress("127.0.0.1", self._created_config["ssh_port"]) if self._created_config else None
        return SSHAddress("127.0.0.1", self.config()["ssh_port"])

    def running_names(self):
        return [path.parent.name for path in self.runtime.state_dir.glob("*/qemu.json")
                if QemuBackend(self.runtime.for_vm(path.parent.name), self.profile).active()]

    def _disk_snapshots(self):
        disk = self.root / "disk.qcow2"
        if not disk.exists():
            return []
        info = json.loads(self.runtime.run([self.tool("qemu-img"), "info", "--force-share", "--output=json", disk], ro=True))
        return [item["name"] for item in info.get("snapshots", [])]

    def snapshots(self):
        return [name for name in self._disk_snapshots() if (self.root / f"vars.{name}.fd").exists()]

    def take_snapshot(self, name):
        # The workflow stops the VM first. Stage firmware before replacing the disk snapshot.
        pending = self.root / f"vars.{name}.fd.tmp"
        if not self.runtime.dry_run:
            shutil.copyfile(self.root / "vars.fd", pending)
            # A failed replacement must not pair a new disk snapshot with old firmware.
            (self.root / f"vars.{name}.fd").unlink(missing_ok=True)
        if name in self._disk_snapshots():
            self.runtime.run([self.tool("qemu-img"), "snapshot", "-d", name, self.root / "disk.qcow2"])
        self.runtime.run([self.tool("qemu-img"), "snapshot", "-c", name, self.root / "disk.qcow2"])
        if not self.runtime.dry_run:
            pending.replace(self.root / f"vars.{name}.fd")

    def restore_snapshot(self, name):
        if not self.runtime.dry_run and name not in self.snapshots():
            raise SystemExit(f"no complete snapshot {name}: both disk and UEFI variables are required")
        self.runtime.run([self.tool("qemu-img"), "snapshot", "-a", name, self.root / "disk.qcow2"])
        if not self.runtime.dry_run:
            shutil.copyfile(self.root / f"vars.{name}.fd", self.root / "vars.fd")


HOST_IP = "192.168.56.1"


class VirtualBoxBackend(Backend):
    supports_baseline = True

    @property
    def network(self):
        return NetworkPolicy(HOST_IP, f"http://{HOST_IP}:3128", True)

    def vbm(self, *args, **kwargs):
        path = shutil.which("VBoxManage") or next((candidate for candidate in (
            r"C:\Program Files\Oracle\VirtualBox\VBoxManage.exe",
            "/Applications/VirtualBox.app/Contents/MacOS/VBoxManage") if os.path.exists(candidate)), None)
        if path is None:
            if not self.runtime.dry_run:
                raise SystemExit("VirtualBox not found: install VirtualBox 7.1+ first")
            if kwargs.get("ro"):
                return ""
        return self.runtime.run([path or "VBoxManage", *args], **kwargs)

    def state(self):
        output = self.vbm("showvminfo", self.runtime.name, "--machinereadable", ro=True, check=False)
        match = re.search(r'^VMState="([^"]+)"', output, re.M)
        return match.group(1) if match else "not found"

    def property(self, name):
        output = self.vbm("guestproperty", "get", self.runtime.name, name, ro=True, check=False)
        return output.split("Value:", 1)[1].strip() if "Value:" in output else ""

    def check_new(self):
        registered = re.findall(r'^"(.+)" \{', self.vbm("list", "vms", ro=True), re.M)
        if self.runtime.name in registered:
            raise SystemExit(f"VM {self.runtime.name} already exists; use --name for another VM")

    def host_only_network(self):
        name = None
        for block in self.vbm("list", "hostonlyifs", ro=True).split("\n\n"):
            fields = {key.strip(): value.strip() for key, _, value in
                      (line.partition(":") for line in block.splitlines())}
            if fields.get("IPAddress") == HOST_IP:
                name = fields["Name"]
                break
        if not name:
            output = self.vbm("hostonlyif", "create")
            name = output.split("'")[1] if "'" in output else "vboxnet0"
            self.vbm("hostonlyif", "ipconfig", name, "--ip", HOST_IP, "--netmask", "255.255.255.0")
        dhcp = self.vbm("list", "dhcpservers", ro=True)
        if name not in dhcp and f"HostInterfaceNetworking-{name}" not in dhcp:
            self.vbm("dhcpserver", "add", "--interface", name, "--server-ip", "192.168.56.2",
                     "--netmask", "255.255.255.0", "--lower-ip", "192.168.56.100",
                     "--upper-ip", "192.168.56.200", "--enable", check=False)
        return name

    def create(self, settings, image, bootstrap, password):
        self.check_new()
        network = self.host_only_network()
        name, vm = self.runtime.name, self.profile.vm
        self.vbm("createvm", "--name", name, "--ostype", vm["os_type"],
                 "--basefolder", self.runtime.state_dir / "vms", "--register")
        self.vbm("modifyvm", name, "--memory", settings.ram_mb, "--cpus", settings.cpus,
                 "--vram", vm["vram_mb"], "--graphicscontroller", vm["graphics_controller"],
                 "--nic1", "nat", "--nic2", "none", "--nic3", "none", "--nic4", "none",
                 "--clipboard-mode", "disabled", "--drag-and-drop", "disabled")
        disk = self.runtime.state_dir / "vms" / name / f"{name}.vdi"
        self.vbm("createmedium", "disk", "--filename", disk, "--size", settings.disk_mb)
        self.vbm("storagectl", name, "--name", "SATA", "--add", "sata")
        self.vbm("storageattach", name, "--storagectl", "SATA", "--port", 0, "--device", 0,
                 "--type", "hdd", "--medium", disk)
        self.vbm("unattended", "install", name, "--iso", image, "--user", self.profile.user,
                 "--user-password", password, "--full-user-name", self.profile.user, "--install-additions",
                 "--post-install-command", bootstrap.post_install_command(),
                 "--start-vm", "headless" if settings.headless else "gui")
        self.runtime.wait("the installer to finish (guest additions report in)",
                          lambda: self.property("/VirtualBox/GuestAdd/Version"), 90)
        if not self.runtime.dry_run:
            time.sleep(60)
        self.stop()
        if settings.baseline:
            self.vbm("modifyvm", name, "--natpf1", f"ssh,tcp,127.0.0.1,{vm['baseline_ssh_port']},,22")
        else:
            self.vbm("modifyvm", name, "--nic1", "hostonly", "--hostonlyadapter1", network)

    def start(self, headless=False):
        if self.state() == "running":
            return
        for _ in range(12):
            try:
                self.vbm("startvm", self.runtime.name, "--type", "headless" if headless else "gui")
                return
            except SystemExit as error:
                if "locked" not in str(error.code):
                    raise
                time.sleep(5)
        raise SystemExit("the VM stayed locked for 60 s")

    def stop(self, force=False):
        if self.runtime.dry_run:
            print("+ VBoxManage controlvm", self.runtime.name, "poweroff" if force else "acpipowerbutton (then poweroff if ignored)")
            return
        if self.state() not in ("running", "paused"):
            return
        if not force and self.state() == "running":
            print("waiting for the VM to power off ...", flush=True)
            for _ in range(6):
                self.vbm("controlvm", self.runtime.name, "acpipowerbutton", check=False)
                for _ in range(6):
                    time.sleep(5)
                    if self.state() not in ("running", "paused"):
                        return
        self.vbm("controlvm", self.runtime.name, "poweroff", check=False)
        self.runtime.wait("the VM to power off", lambda: self.state() not in ("running", "paused"), 1)

    def ssh_address(self):
        address = self.property("/VirtualBox/GuestInfo/Net/0/V4/IP")
        try:
            if ipaddress.ip_address(address) in ipaddress.ip_network("192.168.56.0/24"):
                return SSHAddress(address)
        except ValueError:
            pass
        return None

    def running_names(self):
        return re.findall(r'^"(.+)" \{', self.vbm("list", "runningvms", ro=True), re.M)

    def snapshots(self):
        output = self.vbm("snapshot", self.runtime.name, "list", ro=True, check=False)
        return re.findall(r"Name: (.*?) \(UUID:", output)

    def take_snapshot(self, name):
        if name in self.snapshots():
            self.vbm("snapshot", self.runtime.name, "delete", name)
        self.vbm("snapshot", self.runtime.name, "take", name)

    def restore_snapshot(self, name):
        self.vbm("snapshot", self.runtime.name, "restore", name)


def make_backend(runtime, profile):
    return {"qemu": QemuBackend, "virtualbox": VirtualBoxBackend}[profile.backend](runtime, profile)
