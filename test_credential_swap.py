"""/tmp/mvenv/bin/python test_credential_swap.py  (needs mitmproxy, offline)"""
import importlib
import os
import sys
from pathlib import Path

from mitmproxy.test import tflow

sys.path.insert(0, str(Path(__file__).resolve().parent))


def addon(**env):
    for k in ("SANDBOX_OAUTH_TOKEN", "SANDBOX_ANTHROPIC_KEY"):
        os.environ.pop(k, None)
    os.environ.update(env)
    import proxy_addon
    return importlib.reload(proxy_addon).addons[0]


def flow(host, scheme="https", **headers):
    f = tflow.tflow()
    f.request.host, f.request.scheme = host, scheme
    f.request.headers.clear()
    for k, v in headers.items():
        f.request.headers[k.replace("_", "-")] = v
    return f


def hello(sni):
    class D:
        ignore_connection = False
        context = type("C", (), {"client": type("L", (), {"sni": sni})()})()
    return D()


def main():
    a = addon(SANDBOX_OAUTH_TOKEN="REAL")
    f = flow("api.anthropic.com", authorization="Bearer PLACEHOLDER", x_api_key="PLACEHOLDER")
    a.request(f)
    assert f.request.headers["authorization"] == "Bearer REAL" and "x-api-key" not in f.request.headers
    f = flow("example.com", authorization="Bearer PLACEHOLDER")
    a.request(f)
    assert f.request.headers["authorization"] == "Bearer PLACEHOLDER"       # other hosts untouched
    f = flow("api.anthropic.com", "http", authorization="Bearer PLACEHOLDER")
    a.request(f)
    assert f.request.headers["authorization"] == "Bearer PLACEHOLDER"       # never over plain HTTP
    d = hello("api.anthropic.com"); a.tls_clienthello(d); assert d.ignore_connection is False   # decrypted
    d = hello("example.com"); a.tls_clienthello(d); assert d.ignore_connection is True          # passthrough

    a = addon(SANDBOX_ANTHROPIC_KEY="KEY")
    f = flow("api.anthropic.com", x_api_key="PLACEHOLDER", authorization="Bearer PLACEHOLDER")
    a.request(f)
    assert f.request.headers["x-api-key"] == "KEY" and "authorization" not in f.request.headers

    a = addon()                                                             # no credential: all passthrough
    d = hello("api.anthropic.com"); a.tls_clienthello(d); assert d.ignore_connection is True
    print("ok")


main()
