# Runbook: verify a complete trace, read Envoy response flags

## 1. Verify a complete trace (fund transfer)

Complete traces have a fixed number of spans per journey (measured on this POC):

| Journey | Spans | Hops in the trace |
|---|---|---|
| Fund transfer | **12** | gateway (2) -> payments sidecar in -> `POST /transfer` (Flask) -> 2x `POST` (requests) -> payments sidecar out to core-banking and notify (2) -> notify sidecar in -> `POST /notify` -> `POST` -> notify sidecar out to sms-gateway (passthrough) |
| Balance check | **5** | gateway (2) -> accounts sidecar in -> `POST /balance` -> psycopg2 `SELECT` |
| Card payment | **6** | gateway (2) -> cards sidecar in -> `POST /authorize` -> `POST` -> cards sidecar out to card-switch |
| Login | **4** | gateway (2) -> auth sidecar in -> `POST /login` |

Envoy spans come from the Istio `otel-tracing` provider (service name `<app>.<namespace>`).
App spans come from OTel auto-instrumentation (service name = deployment name).

Steps:

1. Find a recent trace: Grafana > Explore > Tempo > TraceQL
   `{ span.journey = "fund-transfer" }`. The gateway span always carries `journey`.
2. Open it. Check that every hop is present and that the app spans (`POST /transfer`) are
   **children** of the sidecar spans. If the app spans start a new trace instead, the app is
   not propagating `traceparent`: check that the pod has the init container
   `opentelemetry-auto-instrumentation-python` and the env `OTEL_PROPAGATORS=tracecontext,baggage`.
3. Check that `journey` appears on the sidecar spans downstream (payments, notify). If it
   only appears on the gateway spans, baggage is not being forwarded. The Instrumentation CR
   must list the `baggage` propagator.
4. From the terminal, count spans per trace (compare with the table above):
   ```bash
   T=$(curl -s -G localhost:13200/api/search --data-urlencode 'q={ span.journey = "fund-transfer" }' --data-urlencode limit=1 | jq -r '.traces[0].traceID')
   curl -s localhost:13200/api/v2/traces/$T | jq '[.trace.resourceSpans[].scopeSpans[].spans[]] | length'
   ```

Notes:
- **Sampling**: Envoy samples 100%, and the OTel Collector keeps traces with an error, an
  Envoy failure flag or a duration over 1.5 s, plus about 10% of the rest (tail sampling).
  Most healthy traces are therefore *not* stored. That is expected.
- **Partial traces right after a rollout** come from old pods without instrumentation. Wait
  a minute.
- **TCP hops (the database)** have no spans: Envoy does not trace TCP, and the psycopg2
  instrumentation records queries, not failed connects. Use the access log and
  `istio_tcp_connections_opened_total{response_flags!="-"}` for that hop.
- **Tail sampling and late spans**: sidecars and apps flush spans on their own timers, so some
  spans of a trace reach the collector after its sampling decision. The collector's
  `decision_cache` makes them follow that decision. Without it, they are sampled again at 10%
  and most stored traces lose hops. We hit this: fund-transfer traces had 3 of 12 spans.
  Quick test: set the probabilistic policy to 100%. If traces become complete, it is sampling.
- Very recent traces (under about 20 s old) can still be assembling.

## 2. Read Envoy response flags

Every request has a `response_flags` value: in metrics (`istio_requests_total`), in access
logs (Loki: `| response_flags!="-"`; stdout: `kubectl logs <pod> -c istio-proxy`) and on
Envoy spans. `-` means no flag. Read it on the **client-side** Envoy of the failing hop: for
calls to external systems, that is the only sidecar there is.

| Flag | Meaning | Typical cause | Who to call |
|---|---|---|---|
| `UT` | Upstream request timeout | Upstream answered slower than the route timeout (here 3 s for core banking / card switch) | Owner of the upstream system |
| `UF` | Upstream connection failure | Connection refused or connect timeout: host down, port closed, firewall | Owner of the upstream system / network |
| `UH` | No healthy upstream | All endpoints ejected by outlier detection (after repeated `UF`/5xx) or no endpoints at all | Owner of the upstream; the mesh is protecting the caller |
| `URX` | Retry limit exceeded | Shown with `UF`/`UT`: Envoy retried and still failed | As for the paired flag |
| `UC` | Upstream connection termination | Upstream closed the connection mid-request. We saw this when the sidecar sent plaintext to a TLS port | Mesh config (TLS/DestinationRule) or upstream |
| `UR` | Upstream remote reset | Upstream reset the stream (crash, overload) | Owner of the upstream |
| `NR` | No route configured | Wrong host/port or missing VirtualService/ServiceEntry | Mesh config |
| `UO` | Upstream overflow | Circuit breaker (connectionPool limits) tripped | Capacity / config |
| `DC` | Downstream connection termination | The *caller* went away (client timeout) | Usually a symptom, look upstream |
| `LR` | Local reset | Envoy reset the connection itself | Mesh config |

Also check `response_code_details` and `upstream_failure` in the access log. For example,
`delayed_connect_error:_Connection_refused` together with `UF` means the database listener is
down, not that the network is broken.

### How a failure looks in each signal (from the demo scenarios)

| Scenario | Metrics (hop) | Flag | Access log detail | Trace |
|---|---|---|---|---|
| Core banking slow | `payments-svc -> core-banking.poc.internal` 30-40% 5xx (504), p95 = 3 s timeout | `UT` | `upstream_reset_before_response_started` / timeout | 3.0 of 3.0 s on the core banking client span |
| DB down | TCP `accounts-svc -> bank-db.poc.internal` failed connections | `UF,URX` | `delayed_connect_error: Connection refused` | `accounts-svc` app span in ERROR, no DB span |
| Card switch down | `cards-svc -> card-switch.poc.internal` 100% 503 | `UF` then `UH` | connect refused, then no healthy upstream | client span 503 in a few ms |

### p95 reads higher than the timeout?

Istio's latency histogram uses coarse buckets (..., 2500, 5000, 10000 ms), and
`histogram_quantile` interpolates linearly inside a bucket. A hop capped at a 3 s timeout
can therefore show p95 at about 4.7 s. Trust the trace for exact durations, or add finer
buckets with the Telemetry API if you need precise latency SLOs.
