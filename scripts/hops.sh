#!/usr/bin/env bash
# Print every hop with req/s, 5xx %, p95 and Envoy failure flags over the last minute (from Prometheus).
set -euo pipefail
PROM=${PROM:-http://localhost:19090}
q() { curl -s --data-urlencode "query=$1" "$PROM/api/v1/query"; }
python3 -I - "$(q 'sum by (source_workload,destination_service_name)(rate(istio_requests_total{reporter="source"}[1m]))')" \
  "$(q 'sum by (source_workload,destination_service_name)(rate(istio_requests_total{reporter="source",response_code=~"5.."}[1m]))')" \
  "$(q 'histogram_quantile(0.95, sum by (le,source_workload,destination_service_name)(rate(istio_request_duration_milliseconds_bucket{reporter="source"}[1m])))')" \
  "$(q 'sum by (source_workload,destination_service_name,response_flags)(rate(istio_requests_total{reporter="source",response_flags!="-"}[1m])) > 0')" \
  "$(q 'sum by (source_workload,destination_service_name,response_flags)(rate(istio_tcp_connections_opened_total{reporter="source"}[1m])) > 0')" <<'PY'
import json, sys
def m(i, key=lambda r: (r["metric"].get("source_workload"), r["metric"].get("destination_service_name"))):
    return {key(r): float(r["value"][1]) for r in json.loads(sys.argv[i])["data"]["result"]}
rps, err, p95 = m(1), m(2), m(3)
flags = {}
for r in json.loads(sys.argv[4])["data"]["result"]:
    k = (r["metric"]["source_workload"], r["metric"]["destination_service_name"])
    flags.setdefault(k, []).append(r["metric"]["response_flags"])
print(f"{'HOP':58} {'REQ/S':>6} {'5XX':>6} {'P95':>8}  FLAGS")
for k in sorted(rps):
    e = 100 * err.get(k, 0) / rps[k] if rps[k] else 0
    p = p95.get(k, float("nan"))
    print(f"{k[0]+' -> '+k[1]:58} {rps[k]:6.2f} {e:5.1f}% {p:6.0f}ms  {','.join(flags.get(k, [])) or '-'}")
for r in json.loads(sys.argv[5])["data"]["result"]:
    mm = r["metric"]
    print(f"{mm['source_workload']+' -> '+mm['destination_service_name']+' (TCP)':58} {float(r['value'][1]):6.2f} conn/s   flags={mm['response_flags']}")
PY
