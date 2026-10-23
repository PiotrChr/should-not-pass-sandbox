# VM leak report

**19 of 26 tests passed.** 6 FAILED: the sandbox leaked.

Probes run: 26. **Leaks: 6** (1 critical, 5 high). Held: 18. Known and accepted: 2.

Only what the attack suite tried is counted. 0 leaks means none of these probes got through, not that the VM is safe.

## Leaks

- **high**: host port 22: expected blocked, got open
- **high**: host port 7070: expected blocked, got open
- **high**: host port 32400: expected blocked, got open
- **high**: raw tcp to 1.1.1.1:443: expected blocked, got open
- **high**: http without proxy: expected blocked, got reached
- **critical**: direct 192.168.178.1:80: expected blocked, got open

## Held (passed: the attack was blocked)

- PASS host port 3000
- PASS host port 8443
- PASS host proxy port 3128
- PASS direct 10.0.2.2:80
- PASS direct 169.254.169.254:80
- PASS direct 192.168.56.254:80
- PASS router via proxy
- PASS cloud metadata via proxy
- PASS NAT gateway via proxy
- PASS host localhost via proxy
- PASS host-only IP via proxy
- PASS decimal-encoded 127.0.0.1
- PASS hex-encoded 127.0.0.1
- PASS hostname -> 127.0.0.1
- PASS redirect to metadata
- PASS credential files in VM home
- PASS real secrets in environment
- PASS shared folders mounted

## Known and accepted (passed: matches the design)

- PASS docker socket usable (info): blocked (dev is in the docker group by design: root inside the VM, not on the host)
- PASS exfil to public site (info): reached (open mode allows it; an allow list should make this blocked)
