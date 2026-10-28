#!/usr/bin/env python3
"""ShouldNotPass: one CLI for isolated Ubuntu development VMs.

QEMU/HVF on Apple Silicon; VirtualBox on supported x86_64 hosts.
Profiles describe the VM. Both backends use the same provisioning and proxy workflows.
"""
import argparse
from pathlib import Path
import sys


def main(argv=None):
    if sys.version_info < (3, 12):
        raise SystemExit(f"Python 3.12 or newer is required (this is {sys.version.split()[0]}): https://www.python.org/downloads/")
    from snp_vm import Runtime, Sandbox, profile_names, select_profile

    project = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print mutating commands without changing files or VMs")
    parser.add_argument("--name", default="ubuntu-dev", help="VM name, default %(default)s")
    parser.add_argument("--profile", choices=profile_names(project),
                        help="VM profile; defaults to the saved profile or host architecture")
    commands = parser.add_subparsers(dest="cmd", required=True)
    setup = commands.add_parser("setup", help="create the VM and install the shared development environment")
    setup.add_argument("--iso", help="local installer ISO (installer-iso profiles)")
    setup.add_argument("--image", help="local standalone qcow2 cloud disk (cloud-image profiles)")
    setup.add_argument("--password", default="dev", help="guest console password; SSH is key-only (default %(default)s)")
    setup.add_argument("--ram", type=int, help="RAM in MB; defaults to the VM profile")
    setup.add_argument("--cpus", type=int, help="CPU count; defaults to the profile, capped at host CPU count")
    setup.add_argument("--baseline", action="store_true", help="VirtualBox test control: leave NAT enabled, NO sandbox")
    for command in ("up", "provision", "report", "snapshot", "reset", "down", "status"):
        commands.add_parser(command)
    for command in ("setup", "up", "provision", "reset"):
        commands.choices[command].add_argument("--headless", action="store_true", help="start without a VM window")
    args = parser.parse_args(argv)
    try:
        runtime = Runtime(project, args.name, args.dry_run)
        profile = select_profile(runtime, args.profile)
        sandbox = Sandbox(runtime, profile)
        if args.cmd == "setup":
            return sandbox.setup(**{key: getattr(args, key) for key in
                                   ("image", "iso", "ram", "cpus", "password", "headless", "baseline")})
        if args.cmd in ("up", "provision", "reset"):
            return getattr(sandbox, args.cmd)(headless=args.headless)
        return getattr(sandbox, args.cmd)()
    except (ValueError, OSError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
