# Find the red dot: E2E observability POC on kind

A local mirror of the bank's OpenShift design (see `CLAUDE.md`), built from the upstream
projects behind the OpenShift observability stack. Five sample bank services run in an Istio
service mesh. Three "outside the cluster" systems run as Docker containers, and you can break
them on purpose. The live red-dot dashboard, Kiali, Grafana, Tempo and Alertmanager then show
which hop failed and why.

```
 loadgen (mobile / web / ATM)
        |
  Istio ingress gateway --- journey + channel stamped into W3C baggage per route
        |
  auth-svc   accounts-svc ----TCP----> bank-db       (PostgreSQL behind a fault proxy, "Oracle DB")
             payments-svc ---HTTP+TLS-> core-banking (HTTPS mock; sidecar originates TLS)
                  \-> notify-svc -----> sms-gateway  (NOT registered: shows as PassthroughCluster)
             cards-svc -------HTTP----> card-switch  (HTTP mock)
  ---------------- kind cluster ----------------|---- Docker containers on the kind network ----
```

## Prerequisites

| | Needed | Notes |
|---|---|---|
| Docker Desktop | 8+ CPUs, **12+ GB RAM** for Docker, ~20 GB free disk | Steady state uses about 6 to 8 GB RAM. Tested with 10 CPUs and 19.5 GB on an M1 Pro. |
| make, curl, openssl, python3 | macOS/Linux defaults | |
| Free localhost ports | 18080, 18088, 20001, 13000, 13200, 19090, 19093 | Change them in `kind/cluster.yaml` if they are taken. |

Nothing else needs to be installed. `make up` downloads pinned kind, kubectl, helm and istioctl
into `.tools/bin` and uses a project-local kubeconfig and Helm config. Your global tools and
`~/.kube/config` are not touched.

## Setup

```bash
make up        # ~10-15 min the first time (image pulls); safe to re-run
make urls      # print all local URLs
make down      # delete the cluster and the external containers
```

To use `kubectl`/`istioctl` yourself: `eval "$(make -s env)"` (this terminal only).

`make up` runs these phases, which you can also run one at a time:

| Target | What it installs |
|---|---|
| `make cluster` | kind cluster: 1 control plane, 2 workers (Kubernetes 1.36) |
| `make mesh` | Sail Operator, Istio 1.30.5 + Istio CNI, ingress gateway |
| `make apps` | 5 bank services (sidecar-injected, mTLS STRICT) + load generator |
| `make metrics` | kube-prometheus-stack (Prometheus, Alertmanager, Grafana), Istio PodMonitor/ServiceMonitor, alert rules, Kiali 2.27 |
| `make tracing` | cert-manager, Tempo Operator + TempoMonolithic, Loki, OpenTelemetry Operator + Collector, Istio Telemetry API, auto-instrumentation |
| `make externals` | core-banking, card-switch, sms-gateway and bank-db as Docker containers + ServiceEntries |
| `make dashboard` | Red-dot dashboard (backend + live UI) and the Grafana journey dashboard |

## Open the tools

| Tool | URL | What to look at |
|---|---|---|
| **Red-dot dashboard** | http://localhost:18088 | Live topology, fault buttons, journey/service drill-down, detection signals |
| **Kiali** | http://localhost:20001/kiali | Traffic graph: namespaces `bank` + `istio-ingress`, Display > Traffic animation and Response time |
| **Grafana** | http://localhost:13000 | Dashboards > E2E observability > *E2E journey drill-down* (journey variable). Anonymous read-only; admin password: `make grafana-password` |
| **Tempo** | Grafana > Explore > Tempo | TraceQL, e.g. `{ span.journey = "fund-transfer" && status = error }`. Raw API: http://localhost:13200 |
| **Loki** (access logs) | Grafana > Explore > Loki | `{service_name="payments-svc"} \| response_flags!="-"` |
| **Prometheus** | http://localhost:19090 | `istio_requests_total{journey="fund-transfer"}`; Alerts tab: rules `e2e-hops` / `e2e-journeys` |
| **Alertmanager** | http://localhost:19093 | Firing hop and journey alerts |

`make hops` prints every hop with req/s, 5xx %, p95 and Envoy flags in the terminal.

## Demo script (about 10 minutes)

1. **Healthy baseline.** Open the dashboard. Every edge is teal and the status reads
   "All hops healthy". Click **Fund transfer**: only its hops stay lit (gateway, payments,
   core banking, notify, SMS). Click **SMS gateway**: it is an *undeclared* dependency the
   mesh found through `PassthroughCluster`, with nothing installed on that system.
2. **Core banking slow.** Click **Core banking slow** (or `make demo-core-slow`). Core banking
   now answers in 1 to 4 s, and the mesh gives up after 3 s.
   - About 30 s later, `payments-svc to Core banking` turns red, the Core banking node pulses,
     and the upstream hops turn amber. The status reads *Red dot: payments-svc to Core banking*.
   - About 1 to 2 min later the signals show the alerts, a Tempo trace ("3.0 of 3.0 seconds
     spent on payments-svc to Core banking") and the access log ("Envoy flag UT, upstream
     request timeout... Routed to the core banking team").
   - Show the same thing in Kiali (red edge to `core-banking.poc.internal`) and in Grafana's
     journey dashboard with `journey = fund-transfer`.
3. **Oracle DB unreachable.** Click **Oracle DB unreachable** (`make demo-db-down`). The red
   dot moves to `accounts-svc to Oracle DB`. It is a TCP hop, so the evidence is connection
   failures with flag `UF,URX` ("Connection refused"), routed to the DBA team. The balance
   check journey fails; fund transfer is unaffected.
4. **Card switch down.** Click **Card switch down** (`make demo-switch-down`). The flags go
   from `UF` (connect failure) to `UH` (no healthy upstream) once Envoy's outlier detection
   ejects the switch. Only card payments fail.
5. **Recover.** Click **All healthy** (`make reset`). Within about a minute everything is teal
   again and the panel shows *Recovered*.
6. **A new service appears on its own.** Run `make demo-new-service`. It deploys `loans-svc`, a
   new external **Credit bureau** and a new **Loan application** journey. Nothing in the
   dashboard changes, yet within about a minute both nodes appear with a *new* badge and the
   journey gets its own button. Break it with
   `docker exec credit-bureau python faultctl.py set mode=down`: the red dot moves to
   *loans-svc to Credit bureau*, routed to the owner named in its annotation. Remove the
   add-on with `make remove-new-service`.

The buttons and `make demo-*` targets call the same fault API on the mock containers.
More control: `scripts/fault.sh set core-banking error_rate=0.2` or `mode=unreachable`
(the system accepts connections but never answers, like a firewall dropping traffic).
`make fault-status` shows the current faults.

## How the dashboard finds services (like Kiali)

The red-dot dashboard has **no hard-coded topology**. Every 20 s the backend discovers it:

- **Hops** are client-side Istio metrics (`istio_requests_total`,
  `istio_tcp_connections_opened_total`, `reporter="source"`) with traffic in the last 15
  minutes. A hop that stops completely stays visible until it leaves that window, so a dead
  dependency is still drawn.
- **Node types**: gateways (`GATEWAY_WORKLOADS`), mesh services, external systems
  (ServiceEntry hosts, `PassthroughCluster`) and customer channels (the `channel` label).
- **Columns**: channels, gateway, services by call depth from the gateway, outside systems.
  Your browser remembers the order within each column; new nodes go to the bottom of theirs.
- **Journeys** come from the `journey` label on each hop. A TCP hop (the database) has no
  labels, so it is attached to the journeys that reach its client.
- **Display names, descriptions and owner teams** come from annotations on the Kubernetes
  `Service` or Istio `ServiceEntry`. Without them, the raw name is shown.

  ```yaml
  metadata:
    annotations:
      observability.bank/display-name: "Core banking"
      observability.bank/description: "VM, HTTPS"
      observability.bank/owner: "core banking team"   # who the red dot is routed to
      observability.bank/hidden: "true"                # optional: leave it out of the view
  ```

What still needs a deliberate change: **a new journey** has to be added to the fixed journey
list. It lives in three places: the CEL in `manifests/mesh/telemetry.yaml`, the regex in
`manifests/tracing/otel-collector.yaml`, and a gateway route that sets the baggage. This is on
purpose: metric labels must stay low-cardinality. The scenario buttons are POC-only and
drive the four mocks.

## Repository layout

```
Makefile, versions.mk         entry points and pinned versions
kind/cluster.yaml             cluster + localhost port mappings
helm/values/                  values for kube-prometheus-stack, Loki, OpenTelemetry Operator
manifests/mesh/               Istio + IstioCNI CRs (Sail), ingress gateway, Telemetry API
manifests/bank/               sample workload (kustomize; namespace is the parameter), journey routes
manifests/monitoring/         PodMonitor per mesh namespace, istiod ServiceMonitor, alert rules
manifests/kiali/              Kiali CR
manifests/tracing/            TempoMonolithic, OTel Collector, Instrumentation CR
manifests/external/           ServiceEntries, DestinationRules (TLS origination), VirtualServices
manifests/dashboard/          red-dot dashboard deployment (+ read-only RBAC), Grafana journey dashboard
manifests/examples/loans/     add-on used by `make demo-new-service` (new service, external system, journey)
apps/                         bank-svc, loadgen, fault-mock, dashboard (all Python, stdlib or Flask)
scripts/                      one script per phase (called by make)
docs/openshift-mapping.md     every component/manifest and what changes on OpenShift
docs/runbook.md               verify a complete trace; read Envoy response flags
```

## Security notes

- No tokens, kubeconfigs, passwords or real hostnames are in the repo. Generated secrets
  (Grafana admin, DB password) and the throwaway CA live in `.tools/` (gitignored) and in
  Kubernetes Secrets.
- Account and card numbers travel only in request bodies, never in URLs, so they never reach
  access logs, span names or metric labels. Services log masked values (`****1234`). Metric
  labels are only fixed, low-cardinality values (journey, channel).
- All ports are bound to 127.0.0.1. Anonymous Grafana and Kiali access is for local use only.
