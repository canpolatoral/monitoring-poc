"""Load generator: the four customer journeys from mobile, web and ATM channels.

Sends traffic to the ingress gateway at a steady rate. Account and card numbers are
synthetic and are never printed; only aggregate counts are logged.
"""
import os
import random
import signal
import sys
import threading
import time
from collections import Counter

import requests

TARGET = os.environ.get("TARGET", "http://istio-ingressgateway.istio-ingress.svc.cluster.local").rstrip("/")
RPS = float(os.environ.get("RPS", "12"))
WORKERS = int(os.environ.get("WORKERS", "16"))
# Optional journeys, switched on when their service is deployed (e.g. EXTRA_JOURNEYS=loan-application).
EXTRA_JOURNEYS = [j for j in os.environ.get("EXTRA_JOURNEYS", "").split(",") if j]

JOURNEYS = {
    #  journey          method, path
    "login":          ("POST", "/api/login"),
    "balance-check":  ("POST", "/api/balance"),
    "fund-transfer":  ("POST", "/api/transfers"),
    "card-payment":   ("POST", "/api/cards/payments"),
    "loan-application": ("POST", "/api/loans"),
}
OPTIONAL = {"loan-application": {"mobile": 1, "web": 1}}  # journey -> channel weights
# Which journeys each channel produces, with weights.
CHANNELS = {
    "mobile": (0.45, {"login": 3, "balance-check": 4, "fund-transfer": 3}),
    "web":    (0.35, {"login": 3, "balance-check": 3, "fund-transfer": 2}),
    "atm":    (0.20, {"balance-check": 3, "card-payment": 5}),
}
for _j in EXTRA_JOURNEYS:
    for _ch, _w in OPTIONAL.get(_j, {}).items():
        CHANNELS[_ch][1][_j] = _w
USER_AGENTS = {"mobile": "BankApp/7.2 (iOS)", "web": "Mozilla/5.0 InternetBanking", "atm": "ATM-POS-Terminal/3.1"}


def account() -> str:
    return "".join(random.choices("0123456789", k=16))


def body(journey: str) -> dict:
    if journey == "login":
        return {"user": f"user{random.randint(1, 5000)}"}
    if journey == "balance-check":
        return {"account": account()}
    if journey == "fund-transfer":
        return {"from": account(), "to": account(), "amount": round(random.uniform(5, 2500), 2)}
    if journey == "loan-application":
        return {"account": account(), "amount": random.choice([5000, 10000, 25000])}
    return {"card": account(), "amount": round(random.uniform(1, 400), 2)}


stats, lock = Counter(), threading.Lock()


def one_request(session: requests.Session) -> None:
    channel = random.choices(list(CHANNELS), weights=[c[0] for c in CHANNELS.values()])[0]
    mix = CHANNELS[channel][1]
    journey = random.choices(list(mix), weights=list(mix.values()))[0]
    method, path = JOURNEYS[journey]
    headers = {"x-channel": channel, "user-agent": USER_AGENTS[channel]}
    try:
        code = session.request(method, TARGET + path, json=body(journey), headers=headers, timeout=15).status_code
    except requests.RequestException:
        code = 0
    with lock:
        stats[(journey, "ok" if 200 <= code < 300 else f"err{code}")] += 1


def worker(q: "list[float]") -> None:
    s = requests.Session()
    while True:
        with lock:
            due = q.pop(0) if q else None
        if due is None:
            time.sleep(0.01)
            continue
        delay = due - time.time()
        if delay > 0:
            time.sleep(delay)
        one_request(s)


def main() -> None:
    print(f"loadgen: {RPS} req/s to {TARGET}, extra journeys: {EXTRA_JOURNEYS or 'none'}", flush=True)
    queue: list = []
    for _ in range(WORKERS):
        threading.Thread(target=worker, args=(queue,), daemon=True).start()
    nxt, last_report = time.time(), time.time()
    while True:
        nxt += random.expovariate(RPS)  # Poisson arrivals
        with lock:
            if len(queue) < WORKERS * 4:  # shed load instead of queueing forever when the bank is slow
                queue.append(nxt)
        time.sleep(max(0.0, nxt - time.time()))
        if time.time() - last_report >= 30:
            with lock:
                line = " ".join(f"{j}:{r}={n}" for (j, r), n in sorted(stats.items()))
                stats.clear()
            print(f"last 30s: {line}", flush=True)
            last_report = time.time()


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # PID 1: exit promptly on pod shutdown
    main()
