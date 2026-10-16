"""Real network probe from the same runner restrictions used for repository commands."""

import json
import os
import socket
import urllib.error
import urllib.request


def direct_blocked(host, port):
    try:
        with socket.create_connection((host, port), timeout=2):
            return False
    except OSError:
        return True


def proxy_denied(url):
    proxy = os.environ["HTTP_PROXY"]
    # Explicit socket avoids urllib's implicit bypass for localhost/no_proxy targets.
    host, port = proxy.removeprefix("http://").split(":")
    with socket.create_connection((host, int(port)), timeout=5) as connection:
        connection.sendall(f"GET {url} HTTP/1.1\r\nHost: probe\r\n\r\n".encode())
        response = connection.recv(256)
        return response.startswith(b"HTTP/1.1 403 ")


def check():
    checks = {}
    targets = [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.169.254",
        "100.100.100.200",
        "host.docker.internal",
        "gateway.docker.internal",
        "[::1]",
        "[::ffff:8.8.8.8]",
        "postgres",
        "executor-broker",
    ]
    for target in targets:
        checks[f"proxy_denies_{target}"] = proxy_denied(f"http://{target}/")
    checks["direct_public_ipv4_blocked"] = direct_blocked("1.1.1.1", 443)
    checks["direct_public_ipv6_blocked"] = direct_blocked("2606:4700:4700::1111", 443)
    for target in [
        "10.0.0.1",
        "169.254.169.254",
        "host.docker.internal",
        "gateway.docker.internal",
    ]:
        checks[f"direct_{target}_blocked"] = direct_blocked(target, 80)
    # Docker inspect supplies actual broker/DB/control-network addresses.
    for target in json.loads(os.environ.get("BOUNDARY_CONTROL_TARGETS", "[]")):
        checks[f"direct_control_{target['host']}_{target['port']}_blocked"] = direct_blocked(
            target["host"],
            target["port"],
        )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler(
            {
                "http": os.environ["HTTP_PROXY"],
                "https": os.environ["HTTPS_PROXY"],
            }
        )
    )
    for name, url in [
        ("public_http_proxy", "http://example.com/"),
        ("public_https_connect", "https://pypi.org/simple/pip/"),
    ]:
        try:
            with opener.open(url, timeout=15) as response:
                checks[name] = 200 <= response.status < 400
        except (OSError, urllib.error.URLError):
            checks[name] = False
    return {"checks": checks, "passed": all(checks.values())}


if __name__ == "__main__":
    try:
        print(json.dumps(check()))
    except Exception:
        print(json.dumps({"passed": False, "checks": {"probe_error": False}}))
