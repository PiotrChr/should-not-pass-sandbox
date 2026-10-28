"""Shared VM profiles, provisioning and workflows used by the snp CLI and Python callers."""
from dataclasses import dataclass, replace
from pathlib import Path
import base64
import hashlib
import json
import os
import platform
import re
import shlex
import socket
import subprocess
import sys
import time
import tomllib
import urllib.request

from vm_backends import SSHAddress, VMSettings, make_backend


@dataclass(frozen=True)
class Runtime:
    project: Path
    name: str = "ubuntu-dev"
    dry_run: bool = False

    def __post_init__(self):
        object.__setattr__(self, "project", Path(self.project).resolve())
        if not self.name or self.name in (".", "..") or not re.fullmatch(r"[A-Za-z0-9_. -]+", self.name):
            raise ValueError("VM name must contain only letters, numbers, spaces, hyphens, underscores or dots")

    @property
    def state_dir(self):
        return self.project / ".snp"

    @property
    def vm_dir(self):
        return self.state_dir / self.name

    @property
    def windows(self):
        return platform.system() == "Windows"

    def for_vm(self, name):
        return replace(self, name=name)

    def exe(self, name):
        return str(self.state_dir / "venv" / ("Scripts" if self.windows else "bin") /
                   (name + (".exe" if self.windows else "")))

    def run(self, command, *, ro=False, check=True, show=False, timeout=None):
        command = list(map(str, command))
        displayed = command.copy()
        for i, part in enumerate(displayed[:-1]):
            if part in ("--password", "--user-password", "--admin-password"):
                displayed[i + 1] = "<redacted>"
        if self.dry_run and not ro:
            print("+", shlex.join(displayed))
            return ""
        try:
            result = subprocess.run(command, capture_output=not show, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            if not check:
                return ""
            raise SystemExit(f"timed out: {shlex.join(displayed)}")
        if check and result.returncode:
            detail = "" if show else f"\n{(result.stderr or result.stdout).strip()}"
            raise SystemExit(f"failed: {shlex.join(displayed)}{detail}")
        return result.stdout or ""

    def wait(self, what, ready, minutes):
        if self.dry_run:
            print(f"(would wait for {what})")
            return
        print(f"waiting for {what} ...", flush=True)
        deadline = time.monotonic() + minutes * 60
        while not ready():
            if time.monotonic() > deadline:
                raise SystemExit(f"timed out waiting for {what}")
            time.sleep(5)

    def ensure_venv(self):
        if not Path(self.exe("python")).exists():
            self.run([sys.executable, "-m", "venv", self.state_dir / "venv"])
            self.run([self.exe("python"), "-m", "pip", "install", "-q", "mitmproxy", "psutil"])

    def load_env(self):
        env = dict(os.environ)
        path = self.project / ".env"
        for line in path.read_text().splitlines() if path.exists() else []:
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip().strip("'\"")
        return env


@dataclass(frozen=True)
class VMProfile:
    name: str
    backend: str
    host_systems: tuple[str, ...]
    host_machines: tuple[str, ...]
    resources: dict
    image: dict
    guest: dict
    vm: dict

    @property
    def user(self):
        return self.guest["user"]

    @property
    def cloud_image(self):
        return self.image["kind"] == "cloud-image"

    def check_host(self):
        if platform.system() not in self.host_systems or platform.machine().lower() not in self.host_machines:
            raise ValueError(f"profile {self.name} does not support {platform.system()} {platform.machine()}")


def profile_names(project):
    return sorted(p.stem for p in (project / "profiles").glob("*.toml"))


def load_profile(project, name):
    if name not in profile_names(project):
        raise ValueError(f"unknown VM profile: {name}")
    data = tomllib.loads((project / "profiles" / f"{name}.toml").read_text())
    try:
        if data["schema_version"] != 1:
            raise ValueError("unsupported schema_version")
        if data["backend"] not in ("qemu", "virtualbox"):
            raise ValueError("unsupported backend")
        profile = VMProfile(name, data["backend"], tuple(data["host_systems"]),
                            tuple(m.lower() for m in data["host_machines"]), data["resources"], data["image"],
                            data["guest"], data["vm"])
        for key, minimum in (("ram_mb", 1024), ("cpus", 1), ("disk_mb", 1024)):
            if type(profile.resources[key]) is not int or profile.resources[key] < minimum:
                raise ValueError(f"resources.{key} must be an integer >= {minimum}")
        if profile.image["kind"] != {"qemu": "cloud-image", "virtualbox": "installer-iso"}[profile.backend]:
            raise ValueError("image kind does not match backend")
        if not profile.image["base_url"].startswith("https://"):
            raise ValueError("image base_url must use HTTPS")
        filename = profile.image["filename"]
        if not isinstance(filename, str) or filename in ("", ".", "..") or "/" in filename or "\\" in filename:
            raise ValueError("image filename must not contain a directory")
        if not re.fullmatch(r"[a-z_][a-z0-9_-]*", profile.user):
            raise ValueError("invalid guest user")
        required = ({"binary", "machine", "accelerator", "cpu", "display", "graphics_device", "firmware_code", "firmware_vars"}
                    if profile.backend == "qemu" else {"os_type", "graphics_controller", "vram_mb", "baseline_ssh_port"})
        if required - profile.vm.keys():
            raise ValueError(f"missing VM options: {', '.join(sorted(required - profile.vm.keys()))}")
        if type(profile.guest["install_desktop"]) is not bool:
            raise ValueError("guest.install_desktop must be a boolean")
        return profile
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"invalid VM profile {name}: {error}") from error


def read_binding(runtime):
    metadata = runtime.vm_dir / "vm.json"
    if metadata.exists():
        data = json.loads(metadata.read_text())
        if data.get("schema_version") != 1:
            raise ValueError(f"unsupported VM state schema in {metadata}")
        if not isinstance(data.get("profile"), str):
            raise ValueError(f"missing profile in {metadata}")
        if data.get("mode", "sandbox") not in ("sandbox", "baseline"):
            raise ValueError(f"unsupported VM mode in {metadata}")
        return data
    return None


def select_profile(runtime, requested=None):
    binding = read_binding(runtime)
    stored = binding["profile"] if binding else None
    if requested and stored and requested != stored:
        raise ValueError(f"VM {runtime.name} uses profile {stored}; use a different --name for profile {requested}")
    if not requested and not stored:
        stored = ("macos-arm64" if platform.system() == "Darwin" and
                  platform.machine().lower() in ("arm64", "aarch64") else "virtualbox-amd64")
    profile = load_profile(runtime.project, requested or stored)
    if not runtime.dry_run:
        profile.check_host()
    return profile


def save_profile(runtime, profile, *, baseline=False):
    if not runtime.dry_run:
        binding = read_binding(runtime)
        if binding and binding["profile"] != profile.name:
            raise ValueError(f"VM {runtime.name} is already bound to profile {binding['profile']}")
        runtime.vm_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        path = runtime.vm_dir / "vm.json"
        pending = path.with_suffix(".json.tmp")
        pending.write_text(json.dumps({"schema_version": 1, "profile": profile.name,
                                       "mode": "baseline" if baseline else "sandbox"}, indent=2) + "\n")
        pending.replace(path)


def find_image(runtime, profile, given=None):
    if given:
        image = Path(given).expanduser().resolve()
        if not runtime.dry_run and not image.is_file():
            raise SystemExit(f"guest image not found: {image}")
        return image
    if not profile.cloud_image:
        local = next(runtime.project.glob("*.iso"), None)
        if local:
            return local
    filename = profile.image["filename"]
    base = profile.image["base_url"].rstrip("/") + "/"
    dest = runtime.state_dir / filename
    if runtime.dry_run:
        print("+ download and SHA256-check", base + filename)
        return dest
    if dest.exists():
        return dest  # Only complete, verified downloads are promoted to this name.
    runtime.state_dir.mkdir(parents=True, exist_ok=True)
    sums = urllib.request.urlopen(base + "SHA256SUMS", timeout=30).read().decode()
    checksum = next((line.split()[0] for line in sums.splitlines()
                     if line.split() and line.split()[-1].lstrip("*") == filename), None)
    if not checksum:
        raise SystemExit(f"{filename} missing from Ubuntu SHA256SUMS")
    partial = dest.with_suffix(dest.suffix + ".part")
    print("downloading", base + filename, flush=True)
    urllib.request.urlretrieve(base + filename, partial)
    with partial.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != checksum:
        partial.unlink()
        raise SystemExit("guest image checksum mismatch, deleted")
    partial.replace(dest)
    return dest


class Proxy:
    def __init__(self, runtime, network):
        self.runtime, self.network = runtime, network

    @property
    def pid_file(self):
        return self.runtime.state_dir / "proxy.pid"

    def listening(self):
        try:
            socket.create_connection((self.network.listen_host, self.network.port), timeout=1).close()
            return True
        except OSError:
            return False

    def managed(self):
        try:
            pid = int(self.pid_file.read_text())
            if pid <= 0:
                return False
            # psutil lives in the managed venv, so this works on Windows as well as Unix.
            code = "import psutil,sys; print('\\0'.join(psutil.Process(int(sys.argv[1])).cmdline()))"
            command = self.runtime.run([self.runtime.exe("python"), "-c", code, str(pid)], ro=True,
                                       check=False, timeout=5)
            args = command.strip().split("\0")
            return self.runtime.exe("mitmdump") in args and str(self.runtime.project / "proxy_addon.py") in args
        except (OSError, ValueError):
            return False

    def up(self):
        if not self.runtime.dry_run:
            self.runtime.state_dir.mkdir(parents=True, exist_ok=True)
        self.runtime.ensure_venv()
        if self.network.host_firewall:
            command = [self.runtime.exe("python"), self.runtime.project / "firewall.py", "up"]
            # Keep the terminal attached for sudo's password prompt.
            self.runtime.run(command, show=True)
        else:
            print("egress: restricted VM network; only the proxy is forwarded to the guest")
        env = self.runtime.load_env()
        if not (env.get("SANDBOX_OAUTH_TOKEN") or env.get("SANDBOX_ANTHROPIC_KEY")):
            print("note: no SANDBOX_OAUTH_TOKEN in .env, Claude Code in the VM will not authenticate")
        if self.listening():
            if not self.runtime.dry_run and not self.managed():
                raise SystemExit(f"{self.network.listen_host}:{self.network.port} is occupied by an unmanaged service")
            return
        command = [self.runtime.exe("mitmdump"), "--listen-host", self.network.listen_host,
                   "--listen-port", str(self.network.port), "--set", f"confdir={self.runtime.state_dir / 'mitmproxy'}",
                   "-s", str(self.runtime.project / "proxy_addon.py")]
        if self.runtime.dry_run:
            self.runtime.run(command)
        else:
            options = {"creationflags": 0x00000008 | 0x00000200} if self.runtime.windows else {"start_new_session": True}
            with (self.runtime.state_dir / "proxy.out").open("a") as log:
                process = subprocess.Popen(command, env=env, cwd=self.runtime.project, stdout=log,
                                           stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **options)
            self.pid_file.write_text(str(process.pid))
        self.runtime.wait("the proxy to listen", self.listening, 1)

    def down(self):
        if self.runtime.dry_run:
            print("+ stop the managed proxy")
            return
        if not self.pid_file.exists():
            return
        if not self.managed():
            self.pid_file.unlink()  # Never signal a process that merely reused a stale PID.
            return
        try:
            pid = int(self.pid_file.read_text())
            if pid <= 0:
                raise ValueError("invalid proxy PID")
            os.kill(pid, 15)
        except (OSError, ValueError):
            pass
        for _ in range(100):
            if not self.listening():
                break
            time.sleep(0.1)
        else:
            raise SystemExit("the proxy did not stop within 10 seconds; check .snp/proxy.out")
        self.pid_file.unlink()


class GuestBootstrap:
    def __init__(self, runtime, profile, public_key, password):
        self.runtime, self.profile = runtime, profile
        self.public_key, self.password = public_key, password

    def script(self):
        prefix = f"SNP_USER={shlex.quote(self.profile.user)}\nSNP_PUBLIC_KEY={shlex.quote(self.public_key)}\n"
        return prefix + (self.runtime.project / "provisioning" / "bootstrap.sh").read_text()

    def post_install_command(self):
        encoded = base64.b64encode(self.script().encode()).decode()
        return f"printf %s {shlex.quote(encoded)} | base64 -d | bash"

    def cloud_config(self):
        template = json.loads((self.runtime.project / "provisioning" / "cloud-init.json").read_text())
        values = {"@user@": self.profile.user, "@password@": self.password,
                  "@public_key@": self.public_key, "@bootstrap@": self.script()}

        def render(value):
            if isinstance(value, dict):
                return {key: render(item) for key, item in value.items()}
            if isinstance(value, list):
                return [render(item) for item in value]
            return values.get(value, value) if isinstance(value, str) else value
        return render(template)

    def seed_files(self, network):
        return {"user-data": "#cloud-config\n" + json.dumps(self.cloud_config()),
                "meta-data": json.dumps({"instance-id": self.runtime.name, "local-hostname": "snp"}),
                "network-config": json.dumps(network)}


class Guest:
    def __init__(self, runtime, profile, backend):
        self.runtime, self.profile, self.backend = runtime, profile, backend

    def address(self):
        address = self.backend.ssh_address()
        if address is None:
            if self.runtime.dry_run:
                return SSHAddress("<vm ip>")
            raise SystemExit("the guest has no address on the isolated network yet")
        return address

    def options(self, scp=False, address=None):
        address = address or self.address()
        known_hosts = str(self.runtime.vm_dir / "known_hosts").replace("\\", "\\\\").replace('"', '\\"')
        return ["-F", os.devnull, "-i", str(self.runtime.vm_dir / "id_ed25519"),
                "-P" if scp else "-p", str(address.port),
                "-o", "ForwardAgent=no", "-o", "ClearAllForwardings=yes", "-o", "IdentitiesOnly=yes",
                "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=accept-new",
                "-o", f'UserKnownHostsFile="{known_hosts}"']

    def command(self, command):
        address = self.address()
        return ["ssh", *self.options(address=address), f"{self.profile.user}@{address.host}", command]

    def run(self, command, **kwargs):
        return self.runtime.run(self.command(command), **kwargs)

    def copy(self, *paths):
        address = self.address()
        self.runtime.run(["scp", *self.options(scp=True, address=address), *paths,
                          f"{self.profile.user}@{address.host}:"])

    def ready(self):
        if self.backend.state() in ("not found", "poweroff", "aborted"):
            raise SystemExit("the VM stopped while waiting for SSH; check its boot log")
        if self.backend.ssh_address() is None:
            return False
        return self.run("printf snp-ready", check=False, timeout=10) == "snp-ready"

    def wait_ready(self):
        self.runtime.wait("SSH in the isolated guest", self.ready, 15)

    def provision(self, ca):
        self.copy(*[self.runtime.project / f for f in ("setup-guest-proxy.sh", "setup-dev.sh", "report-installed.sh")], ca)
        command = (f"bash setup-guest-proxy.sh {shlex.quote(ca.name)} {shlex.quote(self.backend.network.guest_proxy_url)} && "
                   "set -a && . /etc/environment && set +a && "
                   f"SNP_DESKTOP={int(self.profile.guest['install_desktop'])} bash setup-dev.sh && bash report-installed.sh")
        self.run(command, show=True)

    def report(self):
        self.copy(self.runtime.project / "report-installed.sh")
        self.run("bash report-installed.sh", show=True)


class Sandbox:
    def __init__(self, runtime, profile, backend=None):
        binding = read_binding(runtime)
        if binding and binding["profile"] != profile.name:
            raise ValueError(f"VM {runtime.name} uses profile {binding['profile']}; use a different VM name")
        if backend is not None and (backend.runtime != runtime or backend.profile != profile):
            raise ValueError("backend runtime and profile must match the sandbox")
        self.runtime, self.profile = runtime, profile
        self.backend = backend if backend is not None else make_backend(runtime, profile)
        self.proxy = Proxy(runtime, self.backend.network)
        self.guest = Guest(runtime, profile, self.backend)

    def setup(self, *, image=None, iso=None, ram=None, cpus=None, password="dev", headless=False, baseline=False):
        if (ram is not None and ram < 1024) or (cpus is not None and cpus < 1):
            raise ValueError("setup requires at least 1024 MB RAM and one CPU")
        if baseline and not self.backend.supports_baseline:
            raise ValueError("--baseline is not supported by this profile; restricted networking is always enabled")
        if self.profile.cloud_image and iso:
            raise ValueError("this profile uses a cloud image; use --image FILE")
        if not self.profile.cloud_image and image:
            raise ValueError("this profile uses an installer ISO; use --iso FILE")
        self.backend.check_new()
        self.runtime.ensure_venv()
        source = find_image(self.runtime, self.profile, image or iso)
        if not self.runtime.dry_run:
            self.runtime.vm_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        key = self.runtime.vm_dir / "id_ed25519"
        if not key.exists():
            self.runtime.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", key])
        password_file = self.runtime.vm_dir / "password"
        if not password_file.exists() and not self.runtime.dry_run:
            password_file.write_text(password)
            password_file.chmod(0o600)
        console_password = password_file.read_text() if password_file.exists() else password
        public_key = key.with_suffix(".pub").read_text().strip() if not self.runtime.dry_run else "<public key>"
        settings = VMSettings(ram or self.profile.resources["ram_mb"],
                              cpus or min(self.profile.resources["cpus"], os.cpu_count() or 2),
                              self.profile.resources["disk_mb"], headless, baseline)
        bootstrap = GuestBootstrap(self.runtime, self.profile, public_key, console_password)
        self.backend.create(settings, source, bootstrap, console_password)
        save_profile(self.runtime, self.profile, baseline=baseline)
        if baseline:
            print("baseline VM built with NAT and NO sandbox; SSH uses 127.0.0.1:" + str(self.profile.vm["baseline_ssh_port"]))
            return
        self.provision(headless=headless)

    def _require_sandbox(self):
        binding = read_binding(self.runtime)
        if binding and binding.get("mode") == "baseline":
            raise SystemExit("this VM is an unsandboxed baseline control; start it manually with VBoxManage for control tests")

    def up(self, *, headless=False):
        self._require_sandbox()
        if self.backend.state() == "not found" and not self.runtime.dry_run:
            raise SystemExit(f"VM {self.runtime.name} not found: run `python3 snp.py setup` first")
        self.proxy.up()
        self.backend.start(headless=headless)
        save_profile(self.runtime, self.profile)

    def provision(self, *, headless=False):
        self.up(headless=headless)
        self.guest.wait_ready()
        if self.profile.cloud_image:
            self.guest.run("sudo cloud-init status --wait", show=True)
        ca = self.runtime.state_dir / "mitmproxy" / "mitmproxy-ca-cert.pem"
        self.runtime.wait("the proxy CA", ca.exists, 1)
        self.guest.provision(ca)
        self.backend.stop()
        command = f"python3 snp.py --name {shlex.quote(self.runtime.name)}"
        print(f"\ndone. Next: `{command} up`, check the desktop and tools, then `{command} snapshot`.")

    def report(self):
        self.guest.report()

    def down(self):
        self.backend.stop()
        if any(name != self.runtime.name for name in self.backend.running_names()):
            print("VM stopped; proxy kept running for other VMs")
        else:
            self.proxy.down()

    def snapshot(self):
        self.backend.stop()
        self.backend.take_snapshot("clean")
        print(f"{'would save' if self.runtime.dry_run else 'saved'} snapshot `clean` (all mutable VM state)")

    def reset(self, *, headless=False):
        self._require_sandbox()
        # Check for the snapshot before disrupting a running guest.
        if not self.runtime.dry_run and "clean" not in self.backend.snapshots():
            raise SystemExit("no snapshot yet: get the VM working, then run `python3 snp.py snapshot`")
        self.backend.stop(force=True)
        self.backend.restore_snapshot("clean")
        self.up(headless=headless)

    def status(self):
        print(f"VM {self.runtime.name} ({self.profile.name}, {self.profile.backend}): {self.backend.state()}")
        net = self.backend.network
        print(f"proxy {net.listen_host}:{net.port}: {'listening' if self.proxy.listening() else 'down'}")
        print("snapshots:", ", ".join(self.backend.snapshots()) or "none")
        if self.backend.ssh_address() is not None:
            print("SSH:", shlex.join(self.guest.command("")[:-1]))
