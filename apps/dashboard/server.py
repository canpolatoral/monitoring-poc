"""Red-dot dashboard backend: discovers the topology from Istio metrics (like Kiali), adds
display metadata from Kubernetes annotations, and turns Prometheus, Tempo, Loki and
Alertmanager data into the per-hop / per-journey view the UI draws. It also triggers fault
injection in the external mocks (POC only).

Topology discovery
  * Every client-side hop that carried traffic in the last DISCOVERY_WINDOW is an edge:
    istio_requests_total / istio_tcp_connections_opened_total, reporter="source".
  * Node kinds: gateway (GATEWAY_WORKLOADS), mesh service, external (ServiceEntry hosts,
    PassthroughCluster), customer channel (the `channel` label seen at the gateway).
  * Journeys: the `journey` label on each edge (set at the gateway, carried in baggage).
  * Display metadata: annotations on the Kubernetes Service / Istio ServiceEntry:
      observability.bank/display-name   e.g. "Core banking"
      observability.bank/description    e.g. "VM, HTTPS"
      observability.bank/owner          e.g. "core banking team"   (who gets the red dot)
      observability.bank/hidden: "true" leave the node out of the view

Endpoints
  GET  /                 the UI (static/index.html)
  GET  /api/state        nodes, edges, journeys, totals, red dots, signals, failing traces
  GET  /api/faults       fault state of every external mock
  POST /api/faults       {"scenario": "healthy" | "core" | "db" | "switch"}

Only the Python standard library is used. Connection details come from environment variables.
"""
import json
import os
import signal
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROM = os.environ.get("PROM_URL", "http://kps-prometheus.monitoring.svc.cluster.local:9090")
TEMPO = os.environ.get("TEMPO_URL", "http://tempo-tempo.tracing.svc.cluster.local:3200")
LOKI = os.environ.get("LOKI_URL", "http://loki.logging.svc.cluster.local:3100")
ALERTMANAGER = os.environ.get("ALERTMANAGER_URL", "http://kps-alertmanager.monitoring.svc.cluster.local:9093")
GRAFANA_PUBLIC = os.environ.get("GRAFANA_PUBLIC_URL", "http://localhost:13000")
KIALI_PUBLIC = os.environ.get("KIALI_PUBLIC_URL", "http://localhost:20001/kiali")
PROM_PUBLIC = os.environ.get("PROM_PUBLIC_URL", "http://localhost:19090")
ALERTMANAGER_PUBLIC = os.environ.get("ALERTMANAGER_PUBLIC_URL", "http://localhost:19093")
MOCK_ADMIN = os.environ.get("MOCK_ADMIN_TEMPLATE", "http://{name}.poc.internal:9000")
PORT = int(os.environ.get("PORT", "8088"))
WINDOW = os.environ.get("RATE_WINDOW", "1m")
DISCOVERY_WINDOW = os.environ.get("DISCOVERY_WINDOW", "15m")
GATEWAYS = set(os.environ.get("GATEWAY_WORKLOADS", "istio-ingressgateway").split(","))
NEW_FOR_SECONDS = int(os.environ.get("NEW_BADGE_SECONDS", "900"))
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
ANN = "observability.bank/"

FLAGS = {
    "UT": "upstream request timeout", "UF": "upstream connection failure", "UH": "no healthy upstream",
    "URX": "upstream retry limit exceeded", "UC": "upstream connection termination", "UR": "upstream remote reset",
    "NR": "no route configured", "UO": "upstream overflow (circuit breaker)", "LR": "connection local reset",
    "DC": "downstream connection termination", "RL": "rate limited", "FI": "fault injected", "DI": "delay injected",
}
CHANNEL_NAMES = {"mobile": "Mobile app", "web": "Internet banking", "atm": "ATM and POS"}
PSEUDO_EXTERNAL = {"PassthroughCluster", "BlackHoleCluster"}
IGNORED_DESTINATIONS = {"unknown", ""}
ERR_BAD, ERR_WARN, P95_WARN = 5.0, 1.0, 1000.0  # same thresholds as the alert rules

MOCKS = ["core-banking", "card-switch", "bank-db", "sms-gateway"]
SCENARIOS = {
    "core": ("core-banking", {"latency_ms": 2500, "jitter_ms": 1500}),
    "db": ("bank-db", {"mode": "down"}),
    "switch": ("card-switch", {"mode": "down"}),
}


# ------------------------------------------------------------------ helpers
def http_json(url, data=None, timeout=6, headers=None, context=None):
    h = {"content-type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,
                                 headers=h, method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout, context=context) as r:
        return json.load(r)


def prom(query):
    url = f"{PROM}/api/v1/query?" + urllib.parse.urlencode({"query": query})
    return http_json(url)["data"]["result"]


def fval(v):
    try:
        x = float(v)
        return None if x != x else x  # NaN -> None
    except (TypeError, ValueError):
        return None


def dur(ms):
    """Human duration: '4.7 s' or '12 ms'."""
    return f"{ms / 1000:.1f} s" if ms >= 1000 else f"{ms:.0f} ms"


def explore_url(datasource_uid, dtype, query_fields, rng="now-1h"):
    panes = {"a": {"datasource": datasource_uid,
                   "queries": [{"refId": "A", "datasource": {"type": dtype, "uid": datasource_uid}, **query_fields}],
                   "range": {"from": rng, "to": "now"}}}
    return f"{GRAFANA_PUBLIC}/explore?schemaVersion=1&panes=" + urllib.parse.quote(json.dumps(panes))


def grafana_trace_url(trace_id):
    return explore_url("tempo", "tempo", {"queryType": "traceql", "query": trace_id})


def journey_title(slug):
    return slug.replace("-", " ").capitalize()


class Cache:
    """Tiny TTL cache so many browser tabs do not multiply backend queries."""

    def __init__(self):
        self.data, self.lock = {}, threading.Lock()

    def get(self, key, ttl, fn):
        with self.lock:
            hit = self.data.get(key)
            if hit and time.time() - hit[0] < ttl:
                return hit[1]
        val = fn()
        with self.lock:
            self.data[key] = (time.time(), val)
        return val


cache = Cache()
pool = ThreadPoolExecutor(max_workers=12)


# ------------------------------------------------- Kubernetes display metadata
SA = "/var/run/secrets/kubernetes.io/serviceaccount"


def k8s_list(path):
    token = open(f"{SA}/token").read().strip()
    ctx = ssl.create_default_context(cafile=f"{SA}/ca.crt")
    return http_json(f"https://kubernetes.default.svc{path}", headers={"authorization": f"Bearer {token}"},
                     context=ctx)["items"]


def k8s_metadata():
    """Annotations by node key. Missing permissions or running outside a cluster -> no metadata."""
    meta = {}
    if not os.path.exists(f"{SA}/token"):
        return meta
    try:
        for s in k8s_list("/api/v1/services"):
            m = s["metadata"]
            ann = {k[len(ANN):]: v for k, v in (m.get("annotations") or {}).items() if k.startswith(ANN)}
            if ann:
                meta[f"svc:{m['namespace']}/{m['name']}"] = ann
    except Exception as ex:
        print("k8s services:", ex, file=sys.stderr)
    try:
        for se in k8s_list("/apis/networking.istio.io/v1/serviceentries"):
            ann = {k[len(ANN):]: v for k, v in (se["metadata"].get("annotations") or {}).items() if k.startswith(ANN)}
            for host in se.get("spec", {}).get("hosts", []):
                if ann:
                    meta[f"ext:{host}"] = ann
    except Exception as ex:
        print("k8s serviceentries:", ex, file=sys.stderr)
    return meta


# ---------------------------------------------------------- node identities
def is_external(name):
    return name in PSEUDO_EXTERNAL or ("." in name and not name.endswith(".svc.cluster.local"))


class Identity:
    """Maps metric label sets to node ids, consistently for both ends of a hop.

    Destinations are named by Kubernetes Service (or ServiceEntry host); sources only by
    workload. Rows where the destination workload is known teach us workload -> service.
    """

    def __init__(self, rows):
        self.svc_of = {}
        for m in rows:
            w, ns = m.get("destination_workload"), m.get("destination_service_namespace")
            if w and w != "unknown" and ns and not is_external(m.get("destination_service_name", "")):
                self.svc_of[(ns, w)] = m["destination_service_name"]

    def src(self, m):
        w, ns = m.get("source_workload", "unknown"), m.get("source_workload_namespace", "unknown")
        if w in GATEWAYS:
            return f"gw:{ns}/{w}"
        return f"svc:{ns}/{self.svc_of.get((ns, w), w)}"

    @staticmethod
    def dst(m):
        name, ns = m.get("destination_service_name", ""), m.get("destination_service_namespace", "unknown")
        if name in IGNORED_DESTINATIONS:
            return None
        if is_external(name):
            return f"ext:{name}"
        return f"svc:{ns}/{name}"


BY = "source_workload,source_workload_namespace,destination_service_name,destination_service_namespace"


# -------------------------------------------------------------- discovery
first_seen, STARTED = {}, time.time()


def discover():
    w = DISCOVERY_WINDOW
    q = {
        "http": f'sum by ({BY},destination_workload,journey,channel)(increase(istio_requests_total{{reporter="source"}}[{w}])) > 0',
        "tcp": f'sum by ({BY},destination_workload)(increase(istio_tcp_connections_opened_total{{reporter="source"}}[{w}])) > 0',
    }
    res = {k: f.result() for k, f in {k: pool.submit(prom, v) for k, v in q.items()}.items()}
    meta = cache.get("k8smeta", 60, k8s_metadata)
    ident = Identity([r["metric"] for r in res["http"] + res["tcp"]])

    nodes, edges, journeys = {}, {}, {}

    def node(nid, **kw):
        if nid not in nodes:
            nodes[nid] = {"id": nid, **kw}
        return nodes[nid]

    def add_edge(src, dst, tcp, src_workload, dst_name, dst_ns):
        eid = f"{src}>{dst}"
        if eid not in edges:
            edges[eid] = {"id": eid, "from": src, "to": dst, "tcp": tcp, "srcWorkload": src_workload,
                          "dstName": dst_name, "dstNs": dst_ns}
        return eid

    for kind, rows in (("http", res["http"]), ("tcp", res["tcp"])):
        for r in rows:
            m = r["metric"]
            dst = ident.dst(m)
            if not dst:
                continue
            src = ident.src(m)
            eid = add_edge(src, dst, kind == "tcp", m.get("source_workload"), m.get("destination_service_name"),
                           m.get("destination_service_namespace"))
            j = m.get("journey", "none")
            if kind == "http" and j not in ("none", "other", "unknown"):
                journeys.setdefault(j, set()).add(eid)
            ch = m.get("channel", "none")
            if kind == "http" and src.startswith("gw:") and ch not in ("none", "unknown", ""):
                cid = f"ch:{ch}"
                node(cid, kind="channel", name=CHANNEL_NAMES.get(ch, ch.capitalize()), sub="Customer channel")
                ceid = add_edge(cid, src, False, None, None, None)
                edges[ceid]["channel"] = ch
                edges[ceid]["gateway"] = m.get("source_workload")
                if j not in ("none", "other", "unknown"):
                    journeys[j].add(ceid)

    for e in list(edges.values()):
        for nid in (e["from"], e["to"]):
            if nid in nodes:
                continue
            kind, rest = nid.split(":", 1)
            ann = meta.get(nid, {})
            if kind == "gw":
                node(nid, kind="gateway", name=ann.get("display-name", "Ingress gateway"), sub="Trace starts here")
            elif kind == "ext":
                host = rest
                if host == "PassthroughCluster":
                    node(nid, kind="external", name="Undeclared destinations", sub="PassthroughCluster: not registered",
                         owner=None)
                else:
                    node(nid, kind="external", name=ann.get("display-name", host.split(".")[0]),
                         sub=ann.get("description", "ServiceEntry" + (", TCP" if e["tcp"] else "")),
                         owner=ann.get("owner"), host=host)
            else:
                ns, name = rest.split("/", 1)
                node(nid, kind="service", name=ann.get("display-name", name),
                     sub=ann.get("description", f"namespace {ns}"), owner=ann.get("owner"), namespace=ns)
            if ann.get("hidden") == "true":
                nodes[nid]["hidden"] = True

    hidden = {n for n, v in nodes.items() if v.get("hidden")}
    edges = {k: v for k, v in edges.items() if v["from"] not in hidden and v["to"] not in hidden}
    nodes = {k: v for k, v in nodes.items() if k not in hidden}

    # Depth of mesh services = longest call chain from a gateway (stable left-to-right columns).
    depth = {n: 0 for n, v in nodes.items() if v["kind"] == "gateway"}
    for _ in range(len(nodes)):  # longest path, bounded so call cycles cannot loop forever
        changed = False
        for e in edges.values():
            a, b = e["from"], e["to"]
            if nodes[b]["kind"] == "service" and a in depth and depth.get(b, 0) < depth[a] + 1 <= len(nodes):
                depth[b] = depth[a] + 1
                changed = True
        if not changed:
            break
    for n, v in nodes.items():
        if v["kind"] == "service":
            v["depth"] = max(1, depth.get(n, 1))

    now = time.time()
    for n, v in nodes.items():
        if n not in first_seen:  # nodes present at startup count as known, not new
            first_seen[n] = now if now - STARTED > 60 else 0
        v["isNew"] = bool(first_seen[n]) and now - first_seen[n] < NEW_FOR_SECONDS

    jinfo = {j: {"name": journey_title(j), "edges": sorted(es)} for j, es in sorted(journeys.items())}
    # TCP hops carry no journey label (no headers): attach them to journeys that reach their client.
    for j, info in jinfo.items():
        reached = {edges[e]["to"] for e in info["edges"] if e in edges}
        for e in edges.values():
            if e["tcp"] and e["from"] in reached:
                info["edges"].append(e["id"])
                info.setdefault("inferred", []).append(e["id"])
    return {"nodes": nodes, "edges": edges, "journeys": jinfo}


# ------------------------------------------------------------- live metrics
def collect_metrics():
    w = WINDOW
    q = {
        "req": f'sum by ({BY},destination_workload,journey,channel)(rate(istio_requests_total{{reporter="source"}}[{w}]))',
        "err": f'sum by ({BY},journey,channel)(rate(istio_requests_total{{reporter="source",response_code=~"5.."}}[{w}]))',
        "p95": f'histogram_quantile(0.95, sum by (le,{BY})(rate(istio_request_duration_milliseconds_bucket{{reporter="source"}}[{w}])))',
        "p95j": f'histogram_quantile(0.95, sum by (le,{BY},journey)(rate(istio_request_duration_milliseconds_bucket{{reporter="source"}}[{w}])))',
        "p95c": f'histogram_quantile(0.95, sum by (le,source_workload,source_workload_namespace,channel)(rate(istio_request_duration_milliseconds_bucket{{reporter="source"}}[{w}])))',
        "flags": f'sum by ({BY},response_flags)(rate(istio_requests_total{{reporter="source",response_flags!="-"}}[{w}])) > 0',
        "tcp": f'sum by ({BY},destination_workload,response_flags)(rate(istio_tcp_connections_opened_total{{reporter="source"}}[{w}]))',
    }
    return {k: f.result() for k, f in {k: pool.submit(prom, v) for k, v in q.items()}.items()}


def state_of(err, p95):
    if err is not None and err >= ERR_BAD:
        return "bad"
    if (err is not None and err >= ERR_WARN) or (p95 is not None and p95 >= P95_WARN):
        return "warn"
    return None


def build_view():
    topo = cache.get("topology", 20, discover)
    res = collect_metrics()
    ident = Identity([r["metric"] for r in res["req"] + res["tcp"]])
    # Also learn workload -> service from the discovery window: covers sources whose own
    # destination is failing right now (then destination_workload is "unknown").
    disc_rows = cache.get("identity-rows", 60, lambda: [r["metric"] for r in prom(
        f'sum by ({BY},destination_workload)(increase(istio_requests_total{{reporter="source"}}[{DISCOVERY_WINDOW}])) > 0')])
    ident.svc_of.update(Identity(disc_rows).svc_of)

    def eid_of(m):
        dst = ident.dst(m)
        return f"{ident.src(m)}>{dst}" if dst else None

    acc = {}  # (edge, journey|None) -> [rps, err]
    chan = {}  # (channel edge, journey|None) -> [rps, err]
    errs = {}
    for r in res["err"]:
        m = r["metric"]
        errs[(eid_of(m), m.get("journey"), m.get("channel"))] = fval(r["value"][1]) or 0
    for r in res["req"]:
        m = r["metric"]
        e = eid_of(m)
        if not e:
            continue
        v, er = fval(r["value"][1]) or 0, errs.get((e, m.get("journey"), m.get("channel")), 0)
        for key in ((e, None), (e, m.get("journey"))):
            a = acc.setdefault(key, [0.0, 0.0])
            a[0] += v
            a[1] += er
        if e.startswith("gw:"):
            ce = f"ch:{m.get('channel')}>{ident.src(m)}"
            for key in ((ce, None), (ce, m.get("journey"))):
                a = chan.setdefault(key, [0.0, 0.0])
                a[0] += v
                a[1] += er
    p95 = {eid_of(r["metric"]): fval(r["value"][1]) for r in res["p95"]}
    p95j = {(eid_of(r["metric"]), r["metric"].get("journey")): fval(r["value"][1]) for r in res["p95j"]}
    p95c = {f"ch:{r['metric'].get('channel')}>{ident.src(r['metric'])}": fval(r["value"][1]) for r in res["p95c"]
            if r["metric"].get("source_workload") in GATEWAYS}
    flags = {}
    for r in res["flags"]:
        for f in r["metric"].get("response_flags", "").split(","):
            flags.setdefault(eid_of(r["metric"]), set()).add(f)
    tcp = {}
    for r in res["tcp"]:
        e = eid_of(r["metric"])
        t = tcp.setdefault(e, {"cps": 0.0, "fail": 0.0, "flags": set()})
        v = fval(r["value"][1]) or 0
        t["cps"] += v
        if r["metric"].get("response_flags", "-") != "-":
            t["fail"] += v
            if v > 0:
                t["flags"].update(r["metric"]["response_flags"].split(","))

    def stats(eid, journey=None):
        e = topo["edges"][eid]
        if "channel" in e:
            rps, er = chan.get((eid, journey), [0, 0])
            pv, fl = (p95c.get(eid) if journey is None else None), set()
        elif e["tcp"]:
            t = tcp.get(eid, {"cps": 0, "fail": 0, "flags": set()})
            rps, er, pv, fl = t["cps"], t["fail"], None, t["flags"]
        else:
            rps, er = acc.get((eid, journey), [0, 0])
            pv = p95.get(eid) if journey is None else p95j.get((eid, journey))
            fl = flags.get(eid, set())
        err = (100 * er / rps if rps else 0.0)
        fl = sorted(f for f in fl if f and f != "-")
        st = state_of(err, pv)
        if st is None and fl and rps:
            st = "warn"
        return {"rps": round(rps, 2), "err": round(err, 1), "p95": None if pv is None else round(pv),
                "flags": fl, "state": st}

    edges = {k: {**v, **stats(k)} for k, v in topo["edges"].items()}
    gw_out = [k for k, v in edges.items() if v["from"].startswith("gw:")]
    tr = sum(edges[k]["rps"] for k in gw_out)
    te = sum(edges[k]["rps"] * edges[k]["err"] / 100 for k in gw_out)
    p95all = None
    try:
        r = cache.get("p95all", 5, lambda: prom(
            f'histogram_quantile(0.95, sum by (le)(rate(istio_request_duration_milliseconds_bucket{{reporter="source",source_workload=~"{"|".join(GATEWAYS)}"}}[{WINDOW}])))'))
        p95all = fval(r[0]["value"][1]) if r else None
    except Exception:
        pass

    journeys = {}
    for j, info in topo["journeys"].items():
        je = {k: stats(k, journey=None if edges[k]["tcp"] else j) for k in info["edges"] if k in edges}
        entry = [k for k in info["edges"] if k in edges and edges[k]["from"].startswith("gw:")]
        rps = sum(je[k]["rps"] for k in entry)
        er = sum(je[k]["rps"] * je[k]["err"] / 100 for k in entry)
        ps = [je[k]["p95"] for k in entry if je[k]["p95"] is not None]
        journeys[j] = {**info, "edgeStats": je, "rps": round(rps, 2), "err": round(100 * er / rps, 1) if rps else 0.0,
                       "p95": max(ps) if ps else None}

    # Red dot = the deepest bad edge: a bad edge whose target has no bad outgoing edge.
    bad = {k for k, v in edges.items() if v["state"] == "bad"}
    dots = sorted((k for k in bad if not any(edges[o]["from"] == edges[k]["to"] for o in bad)),
                  key=lambda k: -edges[k]["err"])
    return {"nodes": topo["nodes"], "edges": edges, "journeys": journeys, "redDots": dots,
            "totals": {"rps": round(tr, 2), "err": round(100 * te / tr, 1) if tr else 0.0,
                       "p95": None if p95all is None else round(p95all)}}


# ----------------------------------------------------------- evidence/signals
def tempo_search(traceql, limit=6, since=900):
    now = int(time.time())
    url = f"{TEMPO}/api/search?" + urllib.parse.urlencode({"q": traceql, "limit": limit, "start": now - since, "end": now})
    return http_json(url).get("traces", [])


def failing_traces():
    out = []
    for t in tempo_search("{ status = error } | select(span.journey)", limit=40, since=900):
        journey = None
        for ss in t.get("spanSets", [t.get("spanSet", {})]) or []:
            for sp in (ss or {}).get("spans", []):
                for a in sp.get("attributes", []):
                    if a["key"] == "journey":
                        journey = a["value"].get("stringValue")
        out.append({"traceID": t["traceID"], "journey": journey, "root": t.get("rootTraceName", ""),
                    "service": t.get("rootServiceName", ""), "durationMs": t.get("durationMs", 0),
                    "start": int(t.get("startTimeUnixNano", "0")) // 10**9, "url": grafana_trace_url(t["traceID"])})
    out.sort(key=lambda x: -x["start"])
    return out[:40]


def upstream_pattern(e):
    """Regex fragment matching Envoy's upstream_cluster for this hop."""
    if e["to"].startswith("ext:"):
        return e["dstName"]
    return f"{e['dstName']}.{e['dstNs']}.svc"


def hop_label(view, e):
    return f"{view['nodes'][e['from']]['name']} to {view['nodes'][e['to']]['name']}"


def trace_for_hop(view, eid):
    """Evidence from Tempo for one failing hop: how much of the trace was spent on that hop."""
    e = view["edges"][eid]
    if e["tcp"]:  # Envoy does not trace TCP; show the failing app span instead
        q = f'{{ resource.service.name = "{e["srcWorkload"]}" && status = error }}'
    else:
        q = f'{{ span.upstream_cluster =~ ".*{upstream_pattern(e)}.*" && status = error }}'
    traces = tempo_search(q, limit=3, since=300)
    if not traces:
        return None
    t = traces[0]
    total = t.get("durationMs", 0)
    hop_ms = max((int(sp.get("durationNanos", 0)) / 1e6 for ss in t.get("spanSets", [t.get("spanSet", {})]) or []
                  for sp in (ss or {}).get("spans", [])), default=0)
    hop = hop_label(view, e)
    if e["tcp"]:
        text = f"Tempo: {e['srcWorkload']} span fails in {dur(total)}; Envoy does not trace TCP, the access log shows why"
    elif total >= 1000:
        text = f"Tempo: {dur(hop_ms)} of {dur(total)} spent waiting on {hop}"
    else:  # fails fast: nothing to wait for, the upstream is refused/ejected
        text = f"Tempo: the call {hop} fails after {dur(hop_ms)}; the whole request takes {dur(total)}"
    return {"text": text, "url": grafana_trace_url(t["traceID"])}


def loki_query(e):
    return f'{{service_name="{e["srcWorkload"]}"}} | upstream_cluster=~".*{upstream_pattern(e)}.*" | response_flags!="-" | response_flags!=""'


def loki_flags_for_hop(view, eid):
    e = view["edges"][eid]
    url = f"{LOKI}/loki/api/v1/query?" + urllib.parse.urlencode(
        {"query": f"sum by (response_flags) (count_over_time({loki_query(e)} [2m]))"})
    res = http_json(url)["data"]["result"]
    if not res:
        return None
    top = max(res, key=lambda r: float(r["value"][1]))
    fl = top["metric"].get("response_flags", "")
    n = int(float(top["value"][1]))
    meaning = ", ".join(FLAGS.get(f, f) for f in fl.split(","))
    target = view["nodes"][e["to"]]
    owner = target.get("owner") or f"{target['name']} owners"
    return {"text": f"Envoy flag {fl} ({meaning}) on {n} calls in 2 min. Routed to the {owner}.",
            "url": explore_url("loki", "loki", {"expr": loki_query(e)}, "now-15m")}


def alerts():
    out = []
    for a in http_json(f"{ALERTMANAGER}/api/v2/alerts?active=true&silenced=false&inhibited=false"):
        l = a.get("labels", {})
        if l.get("scope") not in ("hop", "journey"):
            continue
        out.append({"name": l.get("alertname"), "severity": l.get("severity"), "scope": l.get("scope"),
                    "journey": l.get("journey"), "source": l.get("source_workload"),
                    "destination": l.get("destination_service_name"),
                    "summary": a.get("annotations", {}).get("summary", ""), "startsAt": a.get("startsAt")})
    return out


def signals(view, active_alerts):
    """Detection-signals panel: Kiali/metrics, alerts, trace and access-log evidence, live."""
    sig = []
    for eid in view["redDots"]:
        s = view["edges"][eid]
        lat = f", p95 {dur(s['p95'])}" if s["p95"] and s["p95"] >= 100 else (", failing fast" if not s["tcp"] else "")
        what = f"{s['err']:.0f}% connections failing" if s["tcp"] else f"{s['err']:.0f}% errors{lat}"
        sig.append({"tag": "Kiali", "edge": eid, "text": f"Edge {hop_label(view, s)} turned red: {what}",
                    "url": f"{KIALI_PUBLIC}/console/graph/namespaces/?graphType=versionedApp&duration=300",
                    "link": "Open graph"})
    dot_hops = {(view["edges"][d]["srcWorkload"], view["edges"][d]["dstName"]) for d in view["redDots"]}
    ranked = sorted(active_alerts, key=lambda a: ((a["source"], a["destination"]) not in dot_hops,
                                                  a["scope"] != "journey", a["severity"] != "critical", a["name"]))
    for a in ranked[:2]:
        sig.append({"tag": "Alert", "text": f"{a['name']} fired: {a['summary']}", "at": a["startsAt"],
                    "url": ALERTMANAGER_PUBLIC, "link": "Alertmanager"})
    if len(ranked) > 2:
        sig.append({"tag": "Alert", "text": f"{len(ranked) - 2} more alerts firing on the hops and journeys upstream of the red dot",
                    "url": ALERTMANAGER_PUBLIC, "link": "Alertmanager"})
    for eid in view["redDots"]:
        for tag, fn, link in (("Trace", trace_for_hop, "Open trace"), ("Access log", loki_flags_for_hop, "Open logs")):
            try:
                ev = cache.get(f"{tag}:{eid}", 10, lambda: fn(view, eid))
                if ev:
                    sig.append({"tag": tag, "edge": eid, "text": ev["text"], "url": ev["url"], "link": link})
            except Exception as ex:  # evidence is best effort; never break the view
                print(f"{tag}:", ex, file=sys.stderr)
    return sig


def mock_states():
    def one(name):
        try:
            return name, http_json(MOCK_ADMIN.format(name=name) + "/fault", timeout=2)["fault"]
        except Exception as ex:
            return name, {"error": type(ex).__name__}
    return dict(pool.map(one, MOCKS))


def active_scenario(states):
    for sc, (mock, fault) in SCENARIOS.items():
        st = states.get(mock, {})
        if all(st.get(k) == v or (isinstance(v, (int, float)) and st.get(k) == float(v)) for k, v in fault.items()):
            return sc
    return "healthy" if all(s.get("mode") == "up" and not s.get("latency_ms") and not s.get("error_rate")
                             for s in states.values() if "error" not in s) else "custom"


def full_state():
    view = cache.get("view", 2, build_view)
    try:
        act = cache.get("alerts", 5, alerts)
    except Exception as ex:
        print("alertmanager:", ex, file=sys.stderr)
        act = []
    try:
        traces = cache.get("traces", 10, failing_traces)
    except Exception as ex:
        print("tempo:", ex, file=sys.stderr)
        traces = []
    faults = cache.get("faults", 3, mock_states)
    return {**view, "alerts": act, "signals": signals(view, act), "failingTraces": traces,
            "faults": faults, "scenario": active_scenario(faults), "ts": int(time.time()),
            "links": {"kiali": KIALI_PUBLIC, "grafana": GRAFANA_PUBLIC, "prometheus": PROM_PUBLIC,
                      "tempo": explore_url("tempo", "tempo", {"queryType": "traceqlSearch"}, "now-15m")}}


def set_scenario(name):
    for mock in MOCKS:  # always start from healthy so scenarios do not stack
        http_json(MOCK_ADMIN.format(name=mock) + "/reset", {}, timeout=3)
    if name in SCENARIOS:
        mock, fault = SCENARIOS[name]
        http_json(MOCK_ADMIN.format(name=mock) + "/fault", fault, timeout=3)
    cache.data.pop("faults", None)
    return mock_states()


# ------------------------------------------------------------------- server
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(data)))
        self.send_header("cache-control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                with open(os.path.join(STATIC, "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if path == "/api/state":
                return self._send(200, full_state())
            if path == "/api/faults":
                st = mock_states()
                return self._send(200, {"faults": st, "scenario": active_scenario(st)})
            if path == "/healthz":
                return self._send(200, {"ok": True})
            self._send(404, {"error": "not found"})
        except Exception as ex:
            print("error:", repr(ex), file=sys.stderr)
            self._send(502, {"error": f"backend query failed: {type(ex).__name__}"})

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path != "/api/faults":
            return self._send(404, {"error": "not found"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
            name = body.get("scenario")
            if name not in ("healthy", *SCENARIOS):
                return self._send(400, {"error": "scenario must be healthy, core, db or switch"})
            st = set_scenario(name)
            print(f"scenario -> {name}", flush=True)
            self._send(200, {"faults": st, "scenario": active_scenario(st)})
        except Exception as ex:
            print("fault error:", repr(ex), file=sys.stderr)
            self._send(502, {"error": f"could not reach the mocks: {type(ex).__name__}"})


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    print(f"red-dot backend on :{PORT} (prometheus={PROM}, discovery window {DISCOVERY_WINDOW})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
