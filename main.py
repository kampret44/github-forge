"""github-forge entry point.

Lifecycle (per the make.fun harness contract):
  1. Bind an ephemeral port (port 0; never 8080).
  2. POST $MAKEFUN_ENV_URL/register {"port": N}.
  3. Long-poll GET $MAKEFUN_ENV_URL/done with short backoff; exit 0 on
     {"done": true}.
  4. If MAKEFUN_ENV_URL is unset, exit 1.

Serves the forge (git smart HTTP, REST v3, GraphQL, MCP) in one process.
Python 3.13 standard library only.
"""
import json
import os
import socket
import sys
import threading
import time
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from forge.app import App
from forge.httpd import Server


def _register(env_url, port):
    url = env_url.rstrip("/") + "/register"
    payload = json.dumps({"port": port}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST",
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _poll_done(env_url):
    url = env_url.rstrip("/") + "/done"
    while True:
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if data.get("done") is True:
                return
        except Exception:
            pass
        time.sleep(0.25)


def main():
    env_url = os.environ.get("MAKEFUN_ENV_URL")
    if not env_url:
        sys.stderr.write("MAKEFUN_ENV_URL is not set\n")
        return 1
    token = os.environ.get("MAKEFUN_SHOP_TOKEN", "")
    app = App(token)
    server = Server(app.handle, host="127.0.0.1", port=0)
    port = server.start()
    try:
        _register(env_url, port)
    except Exception as exc:
        sys.stderr.write("register failed: %s\n" % exc)
        return 1
    _poll_done(env_url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
