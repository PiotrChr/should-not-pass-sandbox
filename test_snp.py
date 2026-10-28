"""Offline VM/profile/workflow regressions: python3 -m unittest -v test_snp."""
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
import base64
import hashlib
import json
import shutil
import tempfile
import unittest
from unittest import mock

import snp
from snp_vm import Runtime, Sandbox, load_profile, select_profile, save_profile, Guest, GuestBootstrap, find_image, Proxy
from vm_backends import Backend, VMSettings, SSHAddress, QemuBackend, VirtualBoxBackend


PROJECT = Path(__file__).resolve().parent


class ProjectTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.project = Path(directory.name)
        for name in ("profiles", "provisioning"):
            shutil.copytree(PROJECT / name, self.project / name)
        self.runtime = Runtime(self.project, "a VM with spaces")
        self.profile = load_profile(self.project, "macos-arm64")
        patch = mock.patch.object(Runtime, "run", autospec=True, return_value="")
        self.run = patch.start()
        self.addCleanup(patch.stop)
        output = mock.patch("builtins.print")
        self.output = output.start()
        self.addCleanup(output.stop)

    def commands(self):
        return [call.args[1] for call in self.run.call_args_list]


class ProfileTests(ProjectTestCase):
    def test_auto_selects_supported_hosts(self):
        for system, machine, name in (("Darwin", "arm64", "macos-arm64"), ("Darwin", "aarch64", "macos-arm64"),
                                       ("Darwin", "x86_64", "virtualbox-amd64"), ("Linux", "x86_64", "virtualbox-amd64"),
                                       ("Windows", "AMD64", "virtualbox-amd64")):
            with mock.patch("platform.system", return_value=system), mock.patch("platform.machine", return_value=machine):
                self.assertEqual(select_profile(self.runtime).name, name)

    def test_unsupported_host_requires_supported_profile(self):
        with mock.patch("platform.system", return_value="Linux"), mock.patch("platform.machine", return_value="aarch64"):
            with self.assertRaisesRegex(ValueError, "does not support"):
                select_profile(self.runtime)
            profile = select_profile(replace(self.runtime, dry_run=True), "macos-arm64")
            self.assertEqual(profile.backend, "qemu")

    def test_saved_profile_cannot_be_switched_for_existing_vm(self):
        save_profile(self.runtime, self.profile)
        with self.assertRaisesRegex(ValueError, "uses profile macos-arm64"):
            select_profile(self.runtime, "virtualbox-amd64")
        self.assertEqual(json.loads((self.runtime.vm_dir / "vm.json").read_text())["schema_version"], 1)
        with self.assertRaisesRegex(ValueError, "uses profile macos-arm64"):
            Sandbox(self.runtime, load_profile(self.project, "virtualbox-amd64"))

    def test_legacy_virtualbox_credentials_do_not_need_migration(self):
        self.runtime.vm_dir.mkdir(parents=True)
        key = self.runtime.vm_dir / "id_ed25519"
        key.write_bytes(b"existing private key fixture")
        with mock.patch("platform.system", return_value="Linux"), mock.patch("platform.machine", return_value="x86_64"):
            self.assertEqual(select_profile(self.runtime).backend, "virtualbox")
        self.assertEqual(key.read_bytes(), b"existing private key fixture")
        self.assertFalse((self.runtime.vm_dir / "vm.json").exists())

    def test_profile_paths_and_unknown_schema_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown VM profile"):
            load_profile(self.project, "../macos-arm64")
        path = self.project / "profiles" / "macos-arm64.toml"
        path.write_text(path.read_text().replace("schema_version = 1", "schema_version = 99"))
        with self.assertRaisesRegex(ValueError, "schema_version"):
            load_profile(self.project, "macos-arm64")

    def test_invalid_resource_defaults_are_rejected(self):
        path = self.project / "profiles" / "macos-arm64.toml"
        path.write_text(path.read_text().replace("ram_mb = 8192", "ram_mb = -1"))
        with self.assertRaisesRegex(ValueError, "ram_mb"):
            load_profile(self.project, "macos-arm64")

    def test_state_does_not_follow_vm_name_traversal(self):
        for name in ("..", "../other", "a/b", "a\\b", "bad\nname", ""):
            with self.assertRaises(ValueError):
                Runtime(self.project, name)


class SharedWorkflowTests(ProjectTestCase):
    def backends(self):
        for name, adapter in (("macos-arm64", QemuBackend), ("virtualbox-amd64", VirtualBoxBackend)):
            profile = load_profile(self.project, name)
            yield adapter(self.runtime.for_vm(name), profile), profile

    def test_same_up_workflow_loads_proxy_before_start_for_both_backends(self):
        for backend, profile in self.backends():
            with self.subTest(profile=profile.name):
                sandbox = Sandbox(backend.runtime, profile, backend)
                events = []
                with mock.patch.object(backend, "state", return_value="poweroff"), \
                        mock.patch.object(sandbox.proxy, "up", side_effect=lambda: events.append("proxy")), \
                        mock.patch.object(backend, "start", side_effect=lambda **k: events.append("start")):
                    sandbox.up(headless=True)
                self.assertEqual(events, ["proxy", "start"])
                self.assertEqual(json.loads((backend.runtime.vm_dir / "vm.json").read_text())["profile"], profile.name)

    def test_same_reset_workflow_restores_before_start_for_both_backends(self):
        for backend, profile in self.backends():
            sandbox = Sandbox(backend.runtime, profile, backend)
            events = []
            with mock.patch.object(backend, "snapshots", return_value=["clean"]), \
                    mock.patch.object(backend, "stop", side_effect=lambda **k: events.append("stop")), \
                    mock.patch.object(backend, "restore_snapshot", side_effect=lambda n: events.append("restore")), \
                    mock.patch.object(sandbox, "up", side_effect=lambda **k: events.append("up")):
                sandbox.reset(headless=True)
            self.assertEqual(events, ["stop", "restore", "up"])

    def test_missing_snapshot_leaves_running_vm_untouched(self):
        sandbox = Sandbox(self.runtime, self.profile)
        with mock.patch.object(sandbox.backend, "snapshots", return_value=[]), \
                mock.patch.object(sandbox.backend, "stop") as stop:
            with self.assertRaisesRegex(SystemExit, "no snapshot yet"):
                sandbox.reset()
        stop.assert_not_called()

    def test_baseline_control_cannot_be_started_as_a_sandbox(self):
        profile = load_profile(self.project, "virtualbox-amd64")
        save_profile(self.runtime, profile, baseline=True)
        sandbox = Sandbox(self.runtime, profile)
        with mock.patch.object(sandbox.proxy, "up") as proxy, mock.patch.object(sandbox.backend, "start") as start:
            with self.assertRaisesRegex(SystemExit, "unsandboxed baseline control"):
                sandbox.up()
        proxy.assert_not_called()
        start.assert_not_called()

    def test_shared_provisioner_is_used_by_both_backends(self):
        for backend, profile in self.backends():
            sandbox = Sandbox(backend.runtime, profile, backend)
            with mock.patch.object(sandbox, "up"), mock.patch.object(sandbox.guest, "wait_ready"), \
                    mock.patch.object(sandbox.guest, "run") as guest_run, \
                    mock.patch.object(sandbox.guest, "provision") as provision, \
                    mock.patch.object(backend, "stop") as stop, mock.patch.object(Runtime, "wait"):
                sandbox.provision(headless=True)
            provision.assert_called_once_with(backend.runtime.state_dir / "mitmproxy" / "mitmproxy-ca-cert.pem")
            stop.assert_called_once()
            self.assertEqual(guest_run.call_count, int(profile.cloud_image))

    def test_stopping_one_vm_keeps_proxy_for_another(self):
        for backend, profile in self.backends():
            sandbox = Sandbox(backend.runtime, profile, backend)
            with mock.patch.object(backend, "stop"), mock.patch.object(backend, "running_names", return_value=["peer"]), \
                    mock.patch.object(sandbox.proxy, "down") as down:
                sandbox.down()
            down.assert_not_called()

    def test_dry_setup_writes_no_state_for_either_backend(self):
        runtime = replace(self.runtime, dry_run=True)
        for backend_type, name in ((QemuBackend, "macos-arm64"), (VirtualBoxBackend, "virtualbox-amd64")):
            profile = load_profile(self.project, name)
            backend = backend_type(runtime, profile)
            sandbox = Sandbox(runtime, profile, backend)
            with mock.patch.object(backend, "check_new"), mock.patch.object(sandbox, "provision"):
                if isinstance(backend, QemuBackend):
                    sandbox.setup(headless=True, ram=2048, cpus=2)
                    backend.start(headless=True)
                    command = next(cmd for cmd in self.commands() if str(cmd[0]).endswith("qemu-system-aarch64"))
                    self.assertEqual(command[command.index("-m") + 1], "2048")
                    self.assertEqual(command[command.index("-smp") + 1], "2")
                else:
                    with mock.patch.object(backend, "vbm", return_value=""):
                        sandbox.setup(headless=True)
            self.assertFalse(runtime.state_dir.exists())

    def test_invalid_setup_options_fail_before_creating_state(self):
        sandbox = Sandbox(self.runtime, self.profile)
        for options, message in (({"iso": "ubuntu.iso"}, "--image"), ({"baseline": True}, "always enabled"),
                                  ({"ram": 1}, "1024"), ({"cpus": 0}, "one CPU")):
            with self.assertRaisesRegex(ValueError, message):
                sandbox.setup(**options)
            self.assertFalse(self.runtime.state_dir.exists())

    def test_cli_dispatches_named_vm_without_hypervisor_implementation(self):
        with mock.patch.object(snp, "__file__", str(self.project / "snp.py")), \
                mock.patch("snp_vm.Sandbox") as sandbox:
            snp.main(["--dry-run", "--profile", "virtualbox-amd64", "--name", "named-vm", "status"])
        self.assertEqual(sandbox.call_args.args[0].name, "named-vm")
        self.assertEqual(sandbox.call_args.args[1].backend, "virtualbox")
        sandbox.return_value.status.assert_called_once_with()

    def test_old_python_is_refused_before_importing_core(self):
        with mock.patch.object(snp.sys, "version_info", (3, 11, 0)):
            with self.assertRaisesRegex(SystemExit, "3.12"):
                snp.main(["status"])


class GuestAndRuntimeTests(ProjectTestCase):
    def test_ssh_scp_disable_personal_config_and_agent_forwarding(self):
        backend = mock.Mock()
        backend.ssh_address.return_value = SSHAddress("127.0.0.1", 22222)
        guest = Guest(self.runtime, self.profile, backend)
        ssh, scp = guest.options(), guest.options(scp=True)
        self.assertEqual(ssh[ssh.index("-p") + 1], "22222")
        self.assertEqual(scp[scp.index("-P") + 1], "22222")
        self.assertIn("ForwardAgent=no", ssh)
        self.assertIn("ClearAllForwardings=yes", ssh)
        self.assertEqual(ssh[ssh.index("-F") + 1], "/dev/null")
        self.assertIn(f'UserKnownHostsFile="{self.runtime.vm_dir / "known_hosts"}"', ssh)

    def test_bootstrap_template_does_not_interpret_password_as_template_code(self):
        password = '\"\n@bootstrap@ $(touch /tmp/not-executed)'
        bootstrap = GuestBootstrap(self.runtime, self.profile, "ssh-ed25519 PUBLIC", password)
        data = bootstrap.cloud_config()
        self.assertEqual(data["users"][0]["plain_text_passwd"], password)
        self.assertEqual(data["users"][0]["ssh_authorized_keys"], ["ssh-ed25519 PUBLIC"])
        self.assertEqual(data["write_files"][0]["content"], bootstrap.script())
        self.assertNotIn(password, bootstrap.script())

    def test_guest_provisioning_uses_backend_proxy_and_profile_desktop_option(self):
        for name, adapter, proxy, desktop in (("macos-arm64", QemuBackend, "10.0.2.100", 1),
                                              ("virtualbox-amd64", VirtualBoxBackend, "192.168.56.1", 0)):
            profile = load_profile(self.project, name)
            backend = adapter(self.runtime, profile)
            guest = Guest(self.runtime, profile, backend)
            with mock.patch.object(backend, "ssh_address", return_value=SSHAddress("192.168.56.101")):
                guest.provision(Path("ca.pem"))
            remote = self.commands()[-1][-1]
            self.assertIn(f"http://{proxy}:3128", remote)
            self.assertIn(f"SNP_DESKTOP={desktop}", remote)
            self.assertIn("bash setup-dev.sh", remote)

    def test_env_file_parsing_preserves_credentials_without_guest_copy(self):
        (self.project / ".env").write_text("# comment\nA=1\nSANDBOX_OAUTH_TOKEN='sk-x=y'\nB = \"two\"\n")
        env = self.runtime.load_env()
        self.assertEqual(env["A"], "1")
        self.assertEqual(env["SANDBOX_OAUTH_TOKEN"], "sk-x=y")
        self.assertEqual(env["B"], "two")
        data = GuestBootstrap(self.runtime, self.profile, "PUBLIC", "dev").cloud_config()
        self.assertNotIn("sk-x=y", json.dumps(data))

    def test_unmanaged_listener_is_not_reused(self):
        proxy = Proxy(self.runtime, QemuBackend(self.runtime, self.profile).network)
        with mock.patch.object(Runtime, "ensure_venv"), mock.patch.object(proxy, "listening", return_value=True), \
                mock.patch.object(proxy, "managed", return_value=False), mock.patch("builtins.print"):
            with self.assertRaisesRegex(SystemExit, "unmanaged service"):
                proxy.up()

    def test_stale_proxy_pid_does_not_signal_unrelated_process(self):
        proxy = Proxy(self.runtime, QemuBackend(self.runtime, self.profile).network)
        self.runtime.state_dir.mkdir()
        proxy.pid_file.write_text("42")
        with mock.patch.object(proxy, "managed", return_value=False), mock.patch("os.kill") as kill:
            proxy.down()
        kill.assert_not_called()
        self.assertFalse(proxy.pid_file.exists())

    def test_encoded_ipv4_is_normalized_before_macos_dns(self):
        from egress_rules import Denied, Rules
        import ipaddress
        resolver = mock.Mock(side_effect=lambda host, port, **k: [(2, 1, 6, "", (host, port))])
        rules = Rules(resolver=resolver, deny_list=lambda: [ipaddress.ip_network("127.0.0.0/8")])
        with self.assertRaises(Denied):
            rules.resolve("0177.0.0.1", 443)
        self.assertEqual(resolver.call_args.args[0], "127.0.0.1")


class ImageTests(ProjectTestCase):
    def test_corrupt_download_is_not_cached(self):
        filename = self.profile.image["filename"]
        manifest = SimpleNamespace(read=lambda: ("0" * 64 + " *" + filename + "\n").encode())
        def download(url, path):
            path.write_bytes(b"corrupt")
        with mock.patch("urllib.request.urlopen", return_value=manifest), \
                mock.patch("urllib.request.urlretrieve", side_effect=download):
            with self.assertRaisesRegex(SystemExit, "checksum mismatch"):
                find_image(self.runtime, self.profile)
        self.assertFalse((self.runtime.state_dir / filename).exists())
        self.assertFalse((self.runtime.state_dir / (filename + ".part")).exists())

    def test_both_image_types_use_verified_atomic_downloads(self):
        for name in ("macos-arm64", "virtualbox-amd64"):
            profile = load_profile(self.project, name)
            filename = profile.image["filename"]
            payload = b"verified guest image fixture"
            manifest = SimpleNamespace(read=lambda: ("\n" + hashlib.sha256(payload).hexdigest() + " *" + filename + "\n").encode())
            def download(url, path):
                self.assertTrue(str(path).endswith(".part"))
                path.write_bytes(payload)
            with mock.patch("urllib.request.urlopen", return_value=manifest), \
                    mock.patch("urllib.request.urlretrieve", side_effect=download):
                self.assertEqual(find_image(self.runtime, profile).read_bytes(), payload)
            self.assertFalse((self.runtime.state_dir / (filename + ".part")).exists())


class QemuTests(ProjectTestCase):
    def setUp(self):
        super().setUp()
        tools = mock.patch.object(QemuBackend, "tool", side_effect=lambda name: name)
        tools.start()
        self.addCleanup(tools.stop)
        self.backend = QemuBackend(self.runtime, self.profile)
        self.runtime.vm_dir.mkdir(parents=True)
        self.config = {"ram": 4096, "cpus": 2, "ssh_port": 22222, "firmware": "/firmware.fd"}
        self.backend.config_path.write_text(json.dumps(self.config))

    def test_adapter_implements_the_common_contract(self):
        self.assertIsInstance(self.backend, Backend)
        self.assertFalse(self.backend.__abstractmethods__)

    def test_network_has_only_proxy_and_loopback_ssh(self):
        with mock.patch.object(self.backend, "tool", side_effect=lambda name: name):
            command = self.backend.command(self.config, headless=True)
        network = command[command.index("-netdev") + 1]
        self.assertIn("restrict=on,ipv6=off", network)
        self.assertIn("hostfwd=tcp:127.0.0.1:22222-:22", network)
        self.assertIn("guestfwd=tcp:10.0.2.100:3128-cmd:/usr/bin/nc 127.0.0.1 3128", network)
        self.assertEqual(command.count("-netdev"), 1)
        for unsafe in ("-virtfs", "-fsdev", "-spice", "-vnc", "-nic", "-usbdevice"):
            self.assertNotIn(unsafe, command)
        self.assertIn("-nodefaults", command)
        self.assertIn("virt,accel=hvf", command)

    def test_local_datasource_stays_attached_on_every_boot(self):
        command = self.backend.command(self.config)
        self.assertTrue(any("seed.iso" in str(arg) and "readonly=on" in str(arg) for arg in command))

    def test_existing_resources_survive_profile_defaults(self):
        backend = QemuBackend(replace(self.runtime, dry_run=True), self.profile)
        with mock.patch.object(backend, "active", return_value=False):
            backend.start(headless=True)
        command = self.commands()[-1]
        self.assertEqual(command[command.index("-m") + 1], "4096")
        self.assertEqual(command[command.index("-smp") + 1], "2")

    def test_private_monitor_path_is_short_and_vm_specific(self):
        self.assertLess(len(str(self.backend.monitor).encode()), 100)
        other = QemuBackend(self.runtime.for_vm("other-vm"), self.profile)
        self.assertNotEqual(self.backend.monitor, other.monitor)

    def test_monitor_rejects_symlink_before_connecting(self):
        directory = self.project / "unsafe-runtime"
        directory.symlink_to(self.runtime.vm_dir, target_is_directory=True)
        with mock.patch.object(QemuBackend, "control_dir", new_callable=mock.PropertyMock, return_value=directory):
            with self.assertRaisesRegex(SystemExit, "unsafe QMP socket directory"):
                self.backend.qmp("query-status")

    def test_monitor_eof_during_shutdown_counts_as_stopped(self):
        directory = self.project / "private-runtime"
        directory.mkdir(mode=0o700)
        with mock.patch.object(QemuBackend, "control_dir", new_callable=mock.PropertyMock, return_value=directory), \
                mock.patch("vm_backends.socket.socket") as socket:
            stream = socket.return_value.__enter__.return_value.makefile.return_value.__enter__.return_value
            stream.readline.return_value = b""
            self.assertFalse(self.backend.active())

    def test_create_refuses_existing_disk_even_for_library_callers(self):
        with self.assertRaisesRegex(SystemExit, "already exists"):
            self.backend.create(VMSettings(2048, 2, 61440), Path("source.img"), mock.Mock(), "dev")

    def test_restore_requires_firmware_as_well_as_disk_snapshot(self):
        (self.runtime.vm_dir / "disk.qcow2").touch()
        self.run.return_value = json.dumps({"snapshots": [{"name": "clean"}]})
        with self.assertRaisesRegex(SystemExit, "both disk and UEFI"):
            self.backend.restore_snapshot("clean")
        self.assertFalse(any("-a" in command for command in self.commands()))

    def test_restore_restores_uefi_variables(self):
        (self.runtime.vm_dir / "disk.qcow2").touch()
        (self.runtime.vm_dir / "vars.clean.fd").write_bytes(b"clean firmware")
        (self.runtime.vm_dir / "vars.fd").write_bytes(b"planted firmware")
        self.run.return_value = json.dumps({"snapshots": [{"name": "clean"}]})
        self.backend.restore_snapshot("clean")
        self.assertEqual((self.runtime.vm_dir / "vars.fd").read_bytes(), b"clean firmware")
        self.assertIn("-a", self.commands()[-1])

    def test_new_snapshot_replaces_an_orphaned_disk_snapshot(self):
        (self.runtime.vm_dir / "disk.qcow2").touch()
        (self.runtime.vm_dir / "vars.fd").write_bytes(b"new firmware")
        self.run.return_value = json.dumps({"snapshots": [{"name": "clean"}]})
        self.backend.take_snapshot("clean")
        mutations = [command for command in self.commands() if "snapshot" in command]
        self.assertEqual([command[2] for command in mutations], ["-d", "-c"])
        self.assertEqual((self.runtime.vm_dir / "vars.clean.fd").read_bytes(), b"new firmware")

    def test_failed_snapshot_replacement_does_not_advertise_mismatched_firmware(self):
        (self.runtime.vm_dir / "disk.qcow2").touch()
        (self.runtime.vm_dir / "vars.fd").write_bytes(b"new firmware")
        (self.runtime.vm_dir / "vars.clean.fd").write_bytes(b"old firmware")
        snapshots = json.dumps({"snapshots": [{"name": "clean"}]})
        self.run.side_effect = [snapshots, "", SystemExit("snapshot creation failed")]
        with self.assertRaisesRegex(SystemExit, "snapshot creation failed"):
            self.backend.take_snapshot("clean")
        self.run.side_effect = None
        self.run.return_value = snapshots
        self.assertEqual(self.backend.snapshots(), [])

    def test_shared_bootstrap_seed_has_static_network_and_no_host_secret(self):
        self.backend.config_path.unlink()
        captured = {}
        bootstrap = GuestBootstrap(self.runtime, self.profile, "ssh-ed25519 PUBLIC", "dev")
        def execute(runtime, command, **kwargs):
            if command[0] == "hdiutil":
                for path in (self.runtime.vm_dir / "seed").iterdir():
                    captured[path.name] = path.read_text()
                (self.runtime.vm_dir / "seed.iso").touch()
            return json.dumps({"format": "qcow2"}) if "info" in command else ""
        self.run.side_effect = execute
        image = self.project / "cloud.img"
        image.write_bytes(b"image")
        firmware = self.project / "firmware.fd"
        firmware.write_bytes(b"firmware")
        with mock.patch.object(self.backend, "firmware", return_value=(firmware, firmware)), \
                mock.patch.dict("os.environ", {"SANDBOX_OAUTH_TOKEN": "HOST-SECRET"}):
            self.backend.create(VMSettings(2048, 2, 61440), image, bootstrap, "dev")
        data = json.loads(captured["user-data"].split("\n", 1)[1])
        self.assertFalse(data["ssh_pwauth"])
        self.assertEqual(data["users"][0]["ssh_authorized_keys"], ["ssh-ed25519 PUBLIC"])
        self.assertNotIn("HOST-SECRET", " ".join(captured.values()))
        network = json.loads(captured["network-config"])["ethernets"]["sandbox"]
        self.assertEqual(network["addresses"], ["10.0.2.15/24"])
        self.assertFalse(network["dhcp6"])
        self.assertFalse(network["accept-ra"])
        self.assertNotIn("nameservers", network)
        self.assertFalse((self.runtime.vm_dir / "seed").exists())


class VirtualBoxTests(ProjectTestCase):
    def setUp(self):
        super().setUp()
        self.profile = load_profile(self.project, "virtualbox-amd64")
        self.backend = VirtualBoxBackend(self.runtime, self.profile)

    def test_adapter_implements_common_contract(self):
        self.assertIsInstance(self.backend, Backend)
        self.assertFalse(self.backend.__abstractmethods__)

    def test_snapshot_detection_uses_supported_list_command_and_exact_names(self):
        self.run.return_value = "Name: unclean (UUID: 1)\n   Name: clean (UUID: 2) *\n"
        with mock.patch("shutil.which", return_value="VBoxManage"):
            self.assertEqual(self.backend.snapshots(), ["unclean", "clean"])
        command = self.commands()[0]
        self.assertNotIn("--brief", command)
        self.assertEqual(command[1:4], ["snapshot", self.runtime.name, "list"])

    def test_start_retries_only_session_lock_errors(self):
        calls = []
        def vbm(*args, **kwargs):
            if args[0] == "startvm":
                calls.append(args)
                if len(calls) < 3:
                    raise SystemExit("already locked by a session")
            return ""
        with mock.patch.object(self.backend, "vbm", side_effect=vbm), \
                mock.patch.object(self.backend, "state", return_value="poweroff"), mock.patch("time.sleep"):
            self.backend.start(headless=True)
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[-1][-1], "headless")
        with mock.patch.object(self.backend, "vbm", side_effect=SystemExit("boom")), \
                mock.patch.object(self.backend, "state", return_value="poweroff"):
            with self.assertRaisesRegex(SystemExit, "boom"):
                self.backend.start()

    def test_setup_refuses_exact_existing_vm_without_substring_collisions(self):
        with mock.patch.object(self.backend, "vbm", return_value=f'"{self.runtime.name}-other" {{uuid}}\n'):
            self.backend.check_new()
        with mock.patch.object(self.backend, "vbm", return_value=f'"{self.runtime.name}" {{uuid}}\n'):
            with self.assertRaisesRegex(SystemExit, "already exists"):
                self.backend.check_new()

    def test_network_finds_adapter_by_its_address(self):
        listing = ("Name: vboxnet1\nIPAddress: 10.0.0.1\n\n"
                   "Name: VirtualBox Host-Only Ethernet Adapter\nIPAddress: 192.168.56.1\n")
        with mock.patch.object(self.backend, "vbm", side_effect=lambda *a, **k: listing if a[:2] == ("list", "hostonlyifs")
                               else "HostInterfaceNetworking-VirtualBox Host-Only Ethernet Adapter"):
            self.assertEqual(self.backend.host_only_network(), "VirtualBox Host-Only Ethernet Adapter")

    def test_stop_repeats_acpi_then_forces_poweroff(self):
        calls = []
        states = iter(["running"] * 200 + ["poweroff"])
        with mock.patch.object(self.backend, "state", side_effect=lambda: next(states)), \
                mock.patch.object(self.backend, "vbm", side_effect=lambda *a, **k: calls.append(a[2]) or ""), \
                mock.patch("time.sleep"), mock.patch("builtins.print"):
            self.backend.stop()
        self.assertEqual(calls.count("acpipowerbutton"), 6)
        self.assertIn("poweroff", calls)

    def test_stop_returns_when_guest_obeys(self):
        states = iter(["running", "running", "poweroff"])
        with mock.patch.object(self.backend, "state", side_effect=lambda: next(states)), \
                mock.patch.object(self.backend, "vbm") as vbm, mock.patch("time.sleep"), mock.patch("builtins.print"):
            self.backend.stop()
        vbm.assert_called_once_with("controlvm", self.runtime.name, "acpipowerbutton", check=False)

    def test_guest_address_must_be_on_isolated_network(self):
        for address in ("10.0.2.15", "127.0.0.1", "garbage", ""):
            with mock.patch.object(self.backend, "property", return_value=address):
                self.assertIsNone(self.backend.ssh_address())
        with mock.patch.object(self.backend, "property", return_value="192.168.56.101"):
            self.assertEqual(self.backend.ssh_address().host, "192.168.56.101")

    def test_creation_uses_profile_and_shared_bootstrap_before_removing_nat(self):
        calls = []
        bootstrap = GuestBootstrap(self.runtime, self.profile, "ssh-ed25519 PUBLIC", "dev")
        with mock.patch.object(self.backend, "check_new"), \
                mock.patch.object(self.backend, "host_only_network", return_value="vboxnet0"), \
                mock.patch.object(self.backend, "stop"), mock.patch("time.sleep"), \
                mock.patch.object(type(self.runtime), "wait"), \
                mock.patch.object(self.backend, "vbm", side_effect=lambda *a, **k: calls.append(a) or ""):
            self.backend.create(VMSettings(4096, 2, 61440), Path("ubuntu.iso"), bootstrap, "dev")
        install = next(call for call in calls if call[:2] == ("unattended", "install"))
        encoded = install[install.index("--post-install-command") + 1].split()[2]
        self.assertEqual(base64.b64decode(encoded).decode(), bootstrap.script())
        self.assertEqual(install[install.index("--user") + 1], self.profile.user)
        self.assertIn("--user-password", install)
        self.assertEqual(calls[-1], ("modifyvm", self.runtime.name, "--nic1", "hostonly", "--hostonlyadapter1", "vboxnet0"))


if __name__ == "__main__":
    unittest.main()
