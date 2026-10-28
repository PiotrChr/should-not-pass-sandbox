# Security policy

ShouldNotPass exists to contain hostile code, so reports of a way out are the most valuable thing you can send.

## What to report
- The VM reaches the host, localhost, the LAN or any private network.
- Code in the VM bypasses the proxy, or makes the proxy connect to a private address (DNS rebinding, redirects, encoded IPs, IPv6).
- Code in the VM obtains the real Claude token or any other host secret.
- A way for the VM to survive `snp.py reset`.
- A bug in `snp.py`, `firewall.py` or the setup scripts that weakens any of the above.

## Known and accepted (not a report)
- In open mode, the VM can send data to any public site.
- `dev` is in the `docker` group and has passwordless sudo: that is root inside the VM, by design.
- macOS Intel and Windows remain untested. The refactored VirtualBox path still needs live x86_64 qualification.
- A hypervisor escape is a QEMU or VirtualBox bug: report it upstream. If it works because of how this project configures the VM, report it here too.

## Sandbox boundaries

Keep personal accounts and credentials out of the guest. The default proxy allows
public destinations; use `allow.txt` to restrict them. VirtualBox requires the
host firewall loaded by `up`. QEMU uses restricted networking with only the proxy
forwarded outward. The explicit VirtualBox `--baseline` control has unrestricted
NAT and is rejected by normal sandbox `up`/`reset`.

## Tested scope

Apple Silicon validation used macOS 26.5.2, QEMU 11.1.2 and Ubuntu
26.04.1 ARM64. All 35 live probes passed; host-service blocking, proxy-down behavior,
desktop/headless use, Docker, shared provisioning, disk/UEFI reset and concurrent
VM proxy handling were checked. Offline tests cover both adapters. A clean setup
on 2026-10-28 verified fresh host dependencies, image download, full tool
provisioning, desktop login and the VS Code window.
Real Claude authentication was not tested. These checks cover specific behaviors,
not every possible escape or network attack.

## How to report
Use GitHub's private reporting: the repository's **Security** tab, then **Report a vulnerability**. Please do not open a public issue for an escape that still works. Include the steps, the VM name or `setup` options used, and the output of `python3 snp.py status`. Expect a first answer within a week; this is a personal project.

## How to test safely
Test on your own machine. Use throwaway credentials only: never a real token, SSH key or password inside the VM. The attack suite in `attack-suite/` is a harmless starting point.
