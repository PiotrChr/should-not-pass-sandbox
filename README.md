# ShouldNotPass (`snp`): run untrusted repos in a VM that cannot reach your host
![You shall not pass](https://media3.giphy.com/media/v1.Y2lkPTc5MGI3NjExenA4OHpteWVieGZrZnI0ZTZ5ZHdzZWpiYXJqMW5uNzZhOXZxdHk1MCZlcD12MV9pbnRlcm5hbF9naWZfYnlfaWQmY3Q9Zw/S6sZaHdqsGYFO/giphy.gif)

Run untrusted repos inside an Ubuntu VM with internet access through a filtering
proxy. Host/LAN access and host file sharing are blocked by the sandbox configuration.
Reset the VM after each repo. Local VM state and keys live in `.snp/` (git-ignored).

## Requirements

- Python 3.12+ and OpenSSH (`ssh`, `scp`, `ssh-keygen`).
- **Apple Silicon:** native Homebrew QEMU (`brew install qemu python`).
- **x86_64 hosts:** VirtualBox 7.1+. Its host firewall step needs sudo on Linux/macOS
  or an administrator terminal on Windows.
- About 20 GB free disk and 8 GB RAM for the VM; `setup --ram 4096` uses less RAM.

The platform profile is selected automatically. See [SECURITY.md](SECURITY.md)
for tested platforms and limitations.

## Quick start

From this checkout, build and start the VM:

```sh
python3 snp.py setup
python3 snp.py up
```

Setup downloads Ubuntu, configures the sandbox and installs Node, Python, Git,
Chrome, VS Code, Docker and Claude Code. It also installs Insomnia on x86_64.
Console login defaults to `dev` / `dev`; change it with `setup --password`.

Check the desktop and tools, then save the state you want to return to:

```sh
python3 snp.py snapshot
```

For each untrusted repo, run `up`, clone and work **inside the VM**, then run
`reset` to discard its changes and restart from the clean snapshot:

```sh
python3 snp.py up
# Clone and run the repo inside the VM.
python3 snp.py reset
```

Use `python3 snp.py down` when finished.

## Claude Code token (optional)

Run `claude setup-token` on the host and put the result in this checkout's `.env`:

```dotenv
SANDBOX_OAUTH_TOKEN=sk-ant-oat01-...
```

Restart with `down`, then `up`; stop other named VMs first if they are running.
The real token stays on the host. The proxy substitutes it for the guest's
placeholder only when contacting `api.anthropic.com`.

## Commands and options

Run commands as `python3 snp.py COMMAND`:

| Command | Purpose |
|---|---|
| `setup` | Create the VM and install development tools |
| `up` / `down` | Start or stop a session |
| `snapshot` / `reset` | Save or restore the clean state |
| `status` / `report` | Show VM status or installed tools |
| `provision` | Retry a failed tool installation |

Add `--name NAME` **before** the command for a separate VM. `--profile PROFILE`
selects a definition from `profiles/`; existing VMs keep their saved profile.
Add `--headless` to `setup`, `up`, `provision` or `reset` to run without a window.
Use `python3 snp.py setup --help` for RAM, CPU, password and local image options,
or put `--dry-run` before a command to preview it.

## Troubleshooting

- **Setup fails during tool installation:** fix the cause, then run `provision`.
- **No snapshot on reset:** run `snapshot` after checking the clean VM.
- **No internet:** check `status`, then run `up` to reload the proxy/firewall.
- **Mac boot failure:** check `.snp/<name>/qemu.log` and `serial.log`.
- **Claude authentication fails:** check `.env` and restart the VM/proxy.

## Tests

After setup, run the offline tests with the managed environment:

```sh
.snp/venv/bin/python -m unittest -v test_snp
.snp/venv/bin/python test_egress_rules.py
.snp/venv/bin/python test_credential_swap.py
```

On Windows use `.snp/venv/Scripts/python.exe`. See the [CI workflow](.github/workflows/tests.yml)
for environment setup and [attack-suite instructions](attack-suite/README.md) for
live probes, which must run inside the VM.
