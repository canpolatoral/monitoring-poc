# Upstream (kind POC) to OpenShift mapping

Each component and manifest of the local POC is listed with its OpenShift equivalent and
what changes when you move it. Versions are pinned in `versions.mk`. The target is
**OpenShift Service Mesh (OSSM) 3.4** (Istio 1.30, Kiali 2.27), which uses the same
`sailoperator.io/v1` CRDs as the Sail Operator here.

Rule of thumb: **mesh resources (`Istio`, `IstioCNI`, `Telemetry`, `ServiceEntry`,
`DestinationRule`, `VirtualService`, `Gateway`, `PeerAuthentication`), the OpenTelemetry CRs,
`TempoMonolithic`, `PodMonitor`/`ServiceMonitor`/`PrometheusRule` and the Kiali CR carry
over almost unchanged.** What changes is *how operators are installed* (OLM instead of Helm),
*where metrics live* (user workload monitoring + Thanos instead of our own Prometheus) and
*how things are exposed* (Routes instead of NodePorts).

## Platform and mesh

| Local POC (upstream) | OpenShift equivalent | What changes on OpenShift |
|---|---|---|
| kind v0.33, Kubernetes 1.36.4 (`kind/cluster.yaml`) | OpenShift 4.16+ | Nothing to port. Host port mappings and NodePorts are kind-only. |
| Sail Operator 1.31.1 (Helm `sail/sail-operator`) | **Red Hat OpenShift Service Mesh 3** operator (OperatorHub/OLM) | Install from OperatorHub. Same CRDs. |
| `Istio` CR, `version: v1.30.5` (`manifests/mesh/istio.yaml`) | Same `Istio` CR | Use the version OSSM ships (3.4 = Istio 1.30.x). The `openshift` profile is applied automatically. Keep `meshConfig` (extension providers, ALLOW_ANY) unchanged; change the collector service host if you name it differently. |
| `IstioCNI` CR | Same (required on OpenShift) | None. |
| `values.pilot.cni.enabled: true` | Set by the `openshift` profile | Can be removed. |
| Ingress gateway Deployment with gateway injection (`manifests/mesh/ingress-gateway.yaml`) | Same: gateway injection is the OSSM 3 way | Service `NodePort` becomes `ClusterIP` + an OpenShift **Route** (or a LoadBalancer). |
| `Gateway istio-ingress/bank-gateway` (HTTP :80) | Same | Add TLS (`credentialName`), or terminate at the Route (edge/reencrypt). |
| Namespace label `istio-injection=enabled` | Same for revision `default` | With a named revision or `IstioRevisionTag`: `istio.io/rev=<tag>`. On OSSM 3 a namespace must also be in the mesh's discovery selectors if you use them. |
| `PeerAuthentication` STRICT (`manifests/bank/peer-authentication.yaml`) | Same | None. |
| `Telemetry` `istio-system/mesh-default` (`manifests/mesh/telemetry.yaml`) | Same | None. The CEL `tagOverrides` (journey/channel) and access-log filter are standard Istio 1.30. |
| Journey baggage in `VirtualService bank-api` (`manifests/bank/ingress-routes.yaml`) | Same | None. `%REQ(x-channel)%` is Envoy header formatting and works the same. |

## Metrics and Kiali

| Local POC | OpenShift equivalent | What changes |
|---|---|---|
| Prometheus TSDB on a 10 Gi PVC, retention 2 days / 8 GB (`helm/values/kube-prometheus-stack.yaml`) | UWM Prometheus storage | Set `prometheus.volumeClaimTemplate` and `retention` in the `user-workload-monitoring-config` ConfigMap. |
| kube-prometheus-stack 91.9.0 (own Prometheus, Alertmanager, Grafana) | **OpenShift monitoring with user workload monitoring** (`enableUserWorkload: true` in `cluster-monitoring-config`) | Do not install kube-prometheus-stack. Query through **Thanos Querier** `https://thanos-querier.openshift-monitoring.svc:9091` with a bearer token (ServiceAccount with `cluster-monitoring-view`). |
| `PodMonitor istio-proxies-monitor`, one per mesh namespace (`manifests/monitoring/podmonitor-base`, `ns/*`) | Same manifest (copied from the OSSM 3 docs) | UWM only lets a PodMonitor select pods in its own namespace, which is why there is one per namespace already. Add a `ns/<name>` folder per mesh namespace. |
| `ServiceMonitor istiod-monitor` in `istio-system` | Same | None. |
| `PrometheusRule e2e-red-dot` in `istio-system` (`manifests/monitoring/alert-rules.yaml`) | Same CRD, evaluated by UWM (Thanos Ruler) | UWM scopes rules in user namespaces to that namespace's metrics, so place each rule where its metrics live: hop rules in each app namespace (the client sidecar reports there), journey rules in the gateway namespace (`istio-ingress`). Verify the exact rule-evaluation behaviour against the OCP docs for your version. Alerts go to the platform Alertmanager, or to the UWM Alertmanager if enabled. |
| Kiali Operator 2.27 (Helm) + `Kiali` CR (`manifests/kiali/kiali.yaml`) | Kiali Operator provided by Red Hat (OSSM 3) | `auth.strategy: openshift`. `external_services.prometheus.url` = Thanos Querier, with `auth.type: bearer`, `use_kiali_token: true` and `thanos_proxy.enabled: true`. Remove the `kiali-nodeport` Service; the operator creates a Route. Grafana/Tempo URLs point to their Routes. |
| Grafana (in kube-prometheus-stack) + journey dashboard ConfigMap (`manifests/dashboard/grafana-dashboard.yaml`) | **Grafana Operator** (community, or the Grafana you already have) | Turn the dashboard JSON into a `GrafanaDashboard` CR. Data sources: Thanos Querier (bearer token), Tempo gateway/query-frontend Route, LokiStack gateway. |

## Tracing and logs

| Local POC | OpenShift equivalent | What changes |
|---|---|---|
| cert-manager v1.21 (webhook certs for the Tempo/OTel operators) | Not needed: OLM + service-ca issue webhook certs | Do not install. |
| Tempo Operator v0.22.0 (release manifest) + `TempoMonolithic` (`manifests/tracing/tempo.yaml`) | **Tempo Operator** (Red Hat build) + same `TempoMonolithic` | Storage `pv` is fine for a POC. For more, use `TempoStack` with S3 (ODF/MinIO). Multitenancy is optional for monolithic. Expose with a Route instead of `tempo-nodeport`. |
| OpenTelemetry Operator chart 0.124.1 (operator 0.160) + `OpenTelemetryCollector otel` (`manifests/tracing/otel-collector.yaml`) | **Red Hat build of OpenTelemetry** operator + same CR | The Red Hat collector image includes `tail_sampling`, `transform` and `otlphttp`. Check the supported-components list for your version and drop the `collectorImage` override. One replica for tail sampling; to scale out, add a `loadbalancing` exporter tier routing by trace ID. |
| `Instrumentation bank-auto` (`manifests/tracing/instrumentation.yaml`) + pod annotations `instrumentation.opentelemetry.io/inject-python` | Same | Auto-instrumentation is supported for Java, Node.js, Python, .NET, Go (Go needs privileges). For the real Java services, use `inject-java` to get JDBC spans. |
| Istio providers `otel-tracing` / `otel-als` in `meshConfig.extensionProviders` | Same | None. |
| Loki retention 48 h enforced by the compactor (`compactor.retention_enabled`, `helm/values/loki.yaml`) | LokiStack `spec.limits.global.retention` | The Loki Operator runs the compactor for you; set retention days per tenant or stream in the LokiStack CR. |
| Loki 3.6 single binary (Helm `grafana/loki`) | **Loki Operator** + `LokiStack` (object storage) | Logs reach Loki through the OTel Collector's `otlphttp` exporter. With LokiStack, point it at the LokiStack gateway OTLP endpoint (tenant `application`, bearer token), or send Envoy stdout logs with the cluster logging `ClusterLogForwarder` instead. Stream labels: `service_name`, `k8s_namespace_name`. |

## External systems

| Local POC | OpenShift equivalent | What changes |
|---|---|---|
| Docker containers on the `kind` network: `core-banking`, `card-switch`, `sms-gateway`, `bank-db` (+ `bank-db-postgres`) | The real VMs / appliances / Oracle | Not deployed. Nothing is installed on them (Phase 1 rule). |
| CoreDNS zone `poc.internal` (patched by `scripts/externals.sh`) | Corporate DNS | Remove. Use the real FQDNs in `hosts:`. |
| `ServiceEntry` core-banking (HTTP :80 to targetPort 8443) + `DestinationRule` TLS SIMPLE with `credentialName: configmap://bank/core-banking-ca` | Same pattern | Real hostname and port. Put the bank's internal CA in a ConfigMap in the app namespace. **Do not use a Secret `credentialName` without a `workloadSelector`**: Istio 1.30 ignores it on sidecars and the sidecar would send plaintext. `istioctl analyze` warns IST0128 about missing caCertificates; that is a false positive for `configmap://`. |
| `VirtualService` timeouts (3 s) for core-banking / card-switch | Same | Set to the real SLOs. |
| `ServiceEntry` card-switch (HTTP) + `DestinationRule` outlier detection | Same | A real ISO 8583 switch is TCP: declare `protocol: TCP`. You will then only get connection metrics/flags, like the DB. |
| `ServiceEntry` bank-db, TCP 5432 | Oracle: TCP 1521 | Change port/name (`tcp-oracle`). |
| `exportTo: ["."]` on all external resources | Same | Keeps config (and the CA reference) scoped to the namespace that uses the system. |
| sms-gateway not registered (PassthroughCluster) | Same idea | Use Kiali's PassthroughCluster node to find undeclared dependencies, then register them. |

## Dashboard and sample workload (POC only)

| Local POC | OpenShift | Notes |
|---|---|---|
| `red-dot` Deployment in `dashboard` (`manifests/dashboard/dashboard.yaml`) | Same Deployment | Set `PROM_URL` to Thanos Querier and add a bearer token (ServiceAccount + `cluster-monitoring-view`; the backend needs an `Authorization` header, a small change). Tempo/Loki URLs to the query-frontend / LokiStack gateway. **Remove fault injection** (`MOCK_ADMIN_TEMPLATE`, `POST /api/faults`). Expose with a Route behind OAuth proxy. |
| Topology discovery (backend) + annotations `observability.bank/{display-name,description,owner,hidden}` on Services / ServiceEntries | Same | The ClusterRole `red-dot-topology-reader` only needs get/list on `services` and `serviceentries`. Make the owner annotation part of the onboarding checklist, so every red dot names a team. |
| `manifests/bank` (kustomize, `namespace: bank`) | One namespace per team/app | Change `namespace:`, or add an overlay. The real services replace `bank-svc:poc`. |
| Images loaded with `kind load` | Internal registry / Quay | Change `image:` / `imagePullPolicy`. |
| `loadgen` namespace | Real customer channels | Not deployed. |
| Containers run as UID 1000 | `restricted-v2` SCC assigns a random UID | The images do not depend on the UID. |
| Anonymous Grafana/Kiali, NodePorts on 127.0.0.1 | OAuth, Routes | Never expose anonymous access on a bank cluster. |
