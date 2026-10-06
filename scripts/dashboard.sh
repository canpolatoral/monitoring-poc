#!/usr/bin/env bash
# Phase 5: red-dot dashboard (backend + live UI) and the Grafana journey dashboard.
set -euo pipefail; source scripts/lib.sh
log "Building red-dot dashboard image"
docker build -q -t red-dot-dashboard:poc apps/dashboard >/dev/null && kind load docker-image red-dot-dashboard:poc --name "$CLUSTER"
existing=$(kubectl get deploy/red-dot -n dashboard -o name 2>/dev/null || true)
kubectl apply -f manifests/dashboard/dashboard.yaml
[ -n "$existing" ] && kubectl rollout restart deploy/red-dot -n dashboard >/dev/null
kubectl rollout status deploy/red-dot -n dashboard --timeout=120s
log "Grafana dashboard: E2E journey drill-down"
kubectl apply -f manifests/dashboard/grafana-dashboard.yaml
