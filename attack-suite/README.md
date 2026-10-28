# ShouldNotPass attack suite (mock malicious repo)

Looks like a take-home repo; `npm install` runs `probe.py` as a postinstall script, the way a hostile repo would. It only probes and reports. It reads file names, not contents, and sends nothing sensitive.

**Run it only inside the sandbox VM.** It refuses to run without `SNP_VM=1`, so an accidental run on the host does nothing.

```
# on the host: copy it in, then inside the VM
SNP_VM=1 npm install
cat probe-report.json
```

Every probe has an expected result; `FAIL` means the sandbox leaked. Covers: host ports on the host-only address, raw TCP and no-proxy access, router / NAT gateway / metadata / localhost through the proxy, encoded IPs, a hostname that resolves to 127.0.0.1, a redirect to metadata, credential files and secrets in the VM, Docker socket, shared folders, and exfiltration to a public site.

Not automated (check by hand): clipboard in both directions, drag and drop, USB, host filesystem visibility.

Expected today: the host-port probes FAIL until the nft rule on `vboxnet0` is loaded.

On Apple Silicon, the suite reads the guest's `http_proxy` (`http://10.0.2.100:3128`), or an explicit `SNP_PROXY_URL`. Run it from a guest login session, or source `/etc/environment` first. QEMU-specific probes also test its host alias, direct DNS and public UDP DNS. The suite checks direct IPv6 and QEMU/virtio shared filesystems as well. QEMU's `restrict=on` takes the place of a host firewall rule.
