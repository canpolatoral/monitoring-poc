"""CLI for the mock's admin API, used via `docker exec <mock> python faultctl.py ...`.

  python faultctl.py get
  python faultctl.py reset
  python faultctl.py set latency_ms=2500 jitter_ms=1500 error_rate=0.2 mode=down
"""
import json
import sys
import urllib.request

URL = "http://127.0.0.1:9000"


def call(path, body=None):
    req = urllib.request.Request(URL + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"content-type": "application/json"}, method="POST" if body is not None else "GET")
    return json.load(urllib.request.urlopen(req, timeout=5))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "get"
    if cmd == "get":
        out = call("/fault")
    elif cmd == "reset":
        out = call("/reset", {})
    elif cmd == "set":
        out = call("/fault", dict(a.split("=", 1) for a in sys.argv[2:]))
    else:
        sys.exit(__doc__)
    print(json.dumps(out))
