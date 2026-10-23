#!/usr/bin/env python3
"""Close the host to the sandbox VM: from the host-only interface allow only DHCP (udp 67), the proxy and replies.

  python3 firewall.py up|down|status [--dry-run]

Uses the firewall tool of this OS: nft or iptables (Linux), pf (macOS), Windows Firewall (PowerShell).
Needs admin: re-runs itself with sudo on Linux/macOS, asks for an elevated terminal on Windows.
Not persistent across reboots: run `up` again before starting the VM.
Tested on Linux with nft only (dry-run); iptables, pf and Windows are untested.
"""
import os
import platform
import shutil
import subprocess
import sys

HOST_IP, PROXY_PORT = "192.168.56.1", 3128
OS = platform.system()
DRY = "--dry-run" in sys.argv


def sh(cmd, stdin=None, check=True):
    print("+", " ".join(cmd) + ("   <<< (rules below)" if stdin else ""))
    if stdin:
        print(stdin.rstrip())
    if DRY:
        return ""
    r = subprocess.run(cmd, input=stdin, text=True, capture_output=True)
    if check and r.returncode:
        sys.exit(f"failed: {' '.join(cmd)}\n{r.stderr.strip()}")
    return r.stdout


def interface():
    """Name of the host-only adapter that owns HOST_IP, in the form this OS firewall expects."""
    if OS == "Windows":
        out = subprocess.run(["powershell", "-NoProfile", "-Command", f"(Get-NetIPAddress -IPAddress {HOST_IP}).InterfaceAlias"],
                             capture_output=True, text=True).stdout.strip()
        return out or sys.exit(f"no adapter with {HOST_IP}: create the host-only network first")
    import psutil  # ponytail: same dependency as netinfo.py
    for name, addrs in psutil.net_if_addrs().items():
        if any(a.address == HOST_IP for a in addrs):
            return name
    sys.exit(f"no interface with {HOST_IP}: create the host-only network first (`python3 snp.py setup` does it)")


def nft(IF, action):
    if action == "up":
        sh(["nft", "-f", "-"], f"""table inet sandbox
delete table inet sandbox
table inet sandbox {{
  chain input {{
    type filter hook input priority 0; policy accept;
    iifname "{IF}" udp dport 67 accept
    iifname "{IF}" tcp dport {PROXY_PORT} accept
    iifname "{IF}" ct state established,related accept
    iifname "{IF}" drop
  }}
}}
""")  # first two lines make a re-run replace the table instead of failing
    elif action == "down":
        sh(["nft", "delete", "table", "inet", "sandbox"], check=False)
    else:
        print(sh(["nft", "list", "table", "inet", "sandbox"], check=False) or "(not loaded)")


def iptables(IF, action):
    for ipt in [t for t in ("iptables", "ip6tables") if shutil.which(t)]:
        hook = [ipt, "-C", "INPUT", "-i", IF, "-j", "SNP"]
        if action in ("up", "down"):
            sh([ipt, "-D", "INPUT", "-i", IF, "-j", "SNP"], check=False)
            sh([ipt, "-F", "SNP"], check=False)
            sh([ipt, "-X", "SNP"], check=False)
        if action == "up":
            sh([ipt, "-N", "SNP"])
            for rule in (["-p", "udp", "--dport", "67", "-j", "ACCEPT"], ["-p", "tcp", "--dport", str(PROXY_PORT), "-j", "ACCEPT"],
                         ["-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"], ["-j", "DROP"]):
                sh([ipt, "-A", "SNP", *rule])
            sh([ipt, "-I", "INPUT", "-i", IF, "-j", "SNP"])
        elif action == "status":
            print(sh([ipt, "-S", "SNP"], check=False) or f"({ipt}: not loaded)")


def pf(IF, action):
    anchor = "com.apple/snp"  # com.apple/* anchors are evaluated by the stock macOS ruleset, no pf.conf edit
    if action == "up":
        sh(["pfctl", "-E"], check=False)
        sh(["pfctl", "-a", anchor, "-f", "-"], f"""pass in quick on {IF} proto udp to any port 67
pass in quick on {IF} proto tcp to {HOST_IP} port {PROXY_PORT}
block in quick on {IF} all
""")
    elif action == "down":
        sh(["pfctl", "-a", anchor, "-F", "all"], check=False)
    else:
        print(sh(["pfctl", "-a", anchor, "-s", "rules"], check=False) or "(not loaded)")


def windows(IF, action):
    # Windows blocks beat allows, so block every port except the proxy (tcp) and DHCP (udp 67) instead of allowing them
    ps = {"up": f"""Get-NetFirewallRule -DisplayName 'SNP *' -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName 'SNP block tcp' -Direction Inbound -Action Block -Protocol TCP -LocalPort 1-{PROXY_PORT - 1},{PROXY_PORT + 1}-65535 -InterfaceAlias '{IF}'
New-NetFirewallRule -DisplayName 'SNP block udp' -Direction Inbound -Action Block -Protocol UDP -LocalPort 1-66,68-65535 -InterfaceAlias '{IF}'""",
          "down": "Get-NetFirewallRule -DisplayName 'SNP *' -ErrorAction SilentlyContinue | Remove-NetFirewallRule",
          "status": "Get-NetFirewallRule -DisplayName 'SNP *' -ErrorAction SilentlyContinue | Format-Table DisplayName,Enabled,Action"}[action]
    print(sh(["powershell", "-NoProfile", "-Command", ps], check=False))


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 1 or args[0] not in ("up", "down", "status"):
        sys.exit(__doc__)
    action = args[0]
    if OS == "Linux":
        tool = nft if shutil.which("nft") else iptables if shutil.which("iptables") else sys.exit("need nft or iptables")
    elif OS == "Darwin":
        tool = pf
    elif OS == "Windows":
        tool = windows
    else:
        sys.exit(f"unsupported OS: {OS}")
    if not DRY:
        if OS == "Windows":
            import ctypes
            if not ctypes.windll.shell32.IsUserAnAdmin():
                sys.exit("run this from an elevated (administrator) terminal")
        elif os.geteuid() != 0:
            os.execvp("sudo", ["sudo", sys.executable, *sys.argv])
    print(f"{OS}: using {tool.__name__}, interface {(IF := interface())}")
    tool(IF, action)


main()
