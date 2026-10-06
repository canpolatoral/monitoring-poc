#!/usr/bin/env bash
# Phase 2: kube-prometheus-stack, Istio scrape config, Kiali.
set -euo pipefail; source scripts/lib.sh

log "kube-prometheus-stack $KPS_CHART_VERSION"
kubectl create namespace monitoring --dry-run=client -o yaml | kubectl apply -f -
if ! kubectl get secret grafana-admin -n monitoring >/dev/null 2>&1; then
  pw=$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9')
  kubectl create secret generic grafana-admin -n monitoring --from-literal=admin-user=admin --from-literal=admin-password="$pw" >/dev/null
  ok "generated Grafana admin password (read it with: make grafana-password)"
fi
helm_up kps prometheus-community/kube-prometheus-stack "$KPS_CHART_VERSION" monitoring \
  -f helm/values/kube-prometheus-stack.yaml --wait --timeout 10m

log "Istio scrape config (PodMonitor per mesh namespace + istiod ServiceMonitor)"
kubectl apply -k manifests/monitoring

log "Kiali operator $KIALI_CHART_VERSION + Kiali CR"
helm_up kiali-operator kiali/kiali-operator "$KIALI_CHART_VERSION" kiali-operator --wait
kubectl apply -f manifests/kiali/kiali.yaml
retry kubectl get deploy/kiali -n istio-system >/dev/null 2>&1
kubectl rollout status deploy/kiali -n istio-system --timeout=300s
