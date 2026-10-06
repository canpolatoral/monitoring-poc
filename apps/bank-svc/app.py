"""One small image for the five sample bank services; SERVICE_NAME selects the behaviour.

There is deliberately no tracing code here. Spans, context and baggage propagation come
from OpenTelemetry auto-instrumentation injected by the Instrumentation CR. Downstream
targets come from environment variables, so a hop that is not configured yet is skipped.
"""
import logging
import os
import random
import signal
import sys
import time

import requests
from flask import Flask, jsonify, request

SERVICE = os.environ["SERVICE_NAME"]
PORT = int(os.environ.get("PORT", "8080"))
CORE_BANKING_URL = os.environ.get("CORE_BANKING_URL", "").rstrip("/")
CARD_SWITCH_URL = os.environ.get("CARD_SWITCH_URL", "").rstrip("/")
NOTIFY_URL = os.environ.get("NOTIFY_URL", "").rstrip("/")
SMS_GATEWAY_URL = os.environ.get("SMS_GATEWAY_URL", "").rstrip("/")
CREDIT_BUREAU_URL = os.environ.get("CREDIT_BUREAU_URL", "").rstrip("/")
DB_DSN = os.environ.get("DB_DSN", "")
UPSTREAM_TIMEOUT = float(os.environ.get("UPSTREAM_TIMEOUT_SECONDS", "10"))

logging.basicConfig(level=logging.INFO, format=f"%(asctime)s {SERVICE} %(levelname)s %(message)s")
logging.getLogger("werkzeug").setLevel(logging.WARNING)
log = logging.getLogger(SERVICE)

app = Flask(SERVICE)
http = requests.Session()


def mask(account: str) -> str:
    """Never log or return a full account number."""
    a = str(account or "")
    return "****" + a[-4:] if len(a) > 4 else "****"


def work(lo_ms: float, hi_ms: float) -> None:
    time.sleep(random.uniform(lo_ms, hi_ms) / 1000)


def upstream(url: str, payload: dict):
    """POST to a dependency. Returns (status, body); status 0 means no response at all."""
    try:
        r = http.post(url, json=payload, timeout=UPSTREAM_TIMEOUT)
        return r.status_code, (r.json() if r.headers.get("content-type", "").startswith("application/json") else {})
    except requests.RequestException as e:
        log.warning("upstream %s failed: %s", url.split("/")[2], type(e).__name__)
        return 0, {}


@app.get("/healthz")
def healthz():
    return "ok"


# ---------------------------------------------------------------- auth-svc
@app.post("/login")
def login():
    work(5, 25)
    return jsonify(status="ok", token="redacted")


# ------------------------------------------------------------ accounts-svc
@app.post("/balance")
def balance():
    acct = (request.get_json(silent=True) or {}).get("account", "")
    if not DB_DSN:
        work(5, 15)
        return jsonify(account=mask(acct), balance=round(random.uniform(10, 9000), 2), source="cache")
    import psycopg2  # imported lazily so services without a DB don't need it
    try:
        conn = psycopg2.connect(DB_DSN, connect_timeout=3)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT balance FROM accounts WHERE account_ref = %s", (acct[-4:],))
                row = cur.fetchone()
        finally:
            conn.close()
        return jsonify(account=mask(acct), balance=float(row[0]) if row else 0.0, source="db")
    except psycopg2.Error as e:
        log.warning("db query failed for %s: %s", mask(acct), type(e).__name__)
        return jsonify(error="database unavailable"), 503


# ------------------------------------------------------------ payments-svc
@app.post("/transfer")
def transfer():
    body = request.get_json(silent=True) or {}
    work(10, 30)  # validation, limits, fraud rules
    if CORE_BANKING_URL:
        status, _ = upstream(f"{CORE_BANKING_URL}/ledger/transfers",
                             {"from": body.get("from"), "to": body.get("to"), "amount": body.get("amount")})
        if not 200 <= status < 300:
            log.warning("transfer %s -> %s rejected: core banking status %s",
                        mask(body.get("from")), mask(body.get("to")), status or "no response")
            return jsonify(error="core banking unavailable", upstream_status=status), 504 if status in (0, 504) else 502
    if NOTIFY_URL:  # best effort: a failed SMS does not fail the transfer
        upstream(f"{NOTIFY_URL}/notify", {"type": "transfer", "account": mask(body.get("from"))})
    return jsonify(status="booked", reference=f"TRX{random.randint(10**8, 10**9 - 1)}")


# --------------------------------------------------------------- cards-svc
@app.post("/authorize")
def authorize():
    body = request.get_json(silent=True) or {}
    work(5, 15)
    if CARD_SWITCH_URL:
        status, resp = upstream(f"{CARD_SWITCH_URL}/authorize",
                                {"card": mask(body.get("card")), "amount": body.get("amount")})
        if not 200 <= status < 300:
            log.warning("authorization for card %s failed: switch status %s", mask(body.get("card")), status or "no response")
            return jsonify(error="card switch unavailable", upstream_status=status), 504 if status in (0, 504) else 502
        return jsonify(status=resp.get("status", "approved"), auth_code=resp.get("auth_code", "000000"))
    return jsonify(status="approved", auth_code=f"{random.randint(0, 999999):06d}")


# --------------------------------------------------------------- loans-svc
@app.post("/apply")
def apply_loan():
    body = request.get_json(silent=True) or {}
    work(10, 30)
    if CREDIT_BUREAU_URL:
        status, resp = upstream(f"{CREDIT_BUREAU_URL}/score", {"applicant": mask(body.get("account"))})
        if not 200 <= status < 300:
            return jsonify(error="credit bureau unavailable", upstream_status=status), 504 if status in (0, 504) else 502
    if CORE_BANKING_URL:
        status, _ = upstream(f"{CORE_BANKING_URL}/ledger/loans", {"account": mask(body.get("account")), "amount": body.get("amount")})
        if not 200 <= status < 300:
            return jsonify(error="core banking unavailable", upstream_status=status), 504 if status in (0, 504) else 502
    return jsonify(status="approved", reference=f"LN{random.randint(10**7, 10**8 - 1)}")


# -------------------------------------------------------------- notify-svc
@app.post("/notify")
def notify():
    work(3, 10)
    if SMS_GATEWAY_URL:
        status, _ = upstream(f"{SMS_GATEWAY_URL}/send", request.get_json(silent=True) or {})
        if not 200 <= status < 300:
            return jsonify(error="sms gateway unavailable"), 502
    return jsonify(status="queued"), 202


if __name__ == "__main__":
    # PID 1 ignores SIGTERM unless a handler is installed; exit promptly on pod shutdown.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    log.info("starting on :%d", PORT)
    app.run(host="0.0.0.0", port=PORT, threaded=True, use_reloader=False)
