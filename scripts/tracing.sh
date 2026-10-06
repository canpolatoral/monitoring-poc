#!/usr/bin/env bash
# Phase 3: cert-manager, Tempo (operator + TempoMonolithic), Loki, OpenTelemetry Operator +
# Collector, Istio tracing/access-log providers, Telemetry API, journey tagging,
# auto-instrumentation of the bank services.
set -euo pipefail; source scripts/lib.sh

log "cert-manager $CERT_MANAGER_VERSION (webhook certificates for the Tempo/OTel operators)"
helm_up cert-manager jetstack/cert-manager "$CERT_MANAGER_VERSION" cert-manager --set crds.enabled=true --wait

log "Tempo Operator $TEMPO_OPERATOR_VERSION (release manifest; no official Helm chart)"
kubectl apply --server-side -f "https://github.com/grafana/tempo-operator/releases/download/$TEMPO_OPERATOR_VERSION/tempo-operator.yaml" >/dev/null
kubectl rollout status deploy/tempo-operator-controller -n tempo-operator-system --timeout=300s
retry kubectl apply -f manifests/tracing/tempo.yaml
retry kubectl get statefulset/tempo-tempo -n tracing >/dev/null 2>&1
kubectl rollout status statefulset/tempo-tempo -n tracing --timeout=300s

log "Loki $LOKI_CHART_VERSION (single binary)"
helm_up loki grafana/loki "$LOKI_CHART_VERSION" logging -f helm/values/loki.yaml --wait --timeout 10m

log "OpenTelemetry Operator chart $OTEL_OPERATOR_CHART_VERSION + Collector"
helm_up opentelemetry-operator open-telemetry/opentelemetry-operator "$OTEL_OPERATOR_CHART_VERSION" \
  opentelemetry-operator-system -f helm/values/opentelemetry-operator.yaml --wait
retry kubectl apply -f manifests/tracing/otel-collector.yaml
retry kubectl get deploy/otel-collector -n observability >/dev/null 2>&1
kubectl rollout status deploy/otel-collector -n observability --timeout=300s

log "Istio: tracing + access-log providers, Telemetry API (journey/channel labels)"
kubectl apply -f manifests/mesh/istio.yaml
wait_for istio/default "" condition=Ready 300s
kubectl apply -f manifests/mesh/telemetry.yaml

log "Bank: journey routes + auto-instrumentation (restart to inject)"
kubectl apply -k manifests/bank
retry kubectl apply -f manifests/tracing/instrumentation.yaml
kubectl rollout restart deploy -n bank >/dev/null
kubectl rollout restart deploy/istio-ingressgateway -n istio-ingress >/dev/null
for d in auth-svc accounts-svc payments-svc cards-svc notify-svc; do kubectl rollout status deploy/$d -n bank --timeout=180s; done
kubectl rollout status deploy/istio-ingressgateway -n istio-ingress --timeout=180s
