#!/usr/bin/env bash
# Phase 1: build the sample images, load them into kind, deploy the bank and the load generator.
set -euo pipefail; source scripts/lib.sh

log "Building images"
docker build -q -t bank-svc:poc apps/bank-svc
docker build -q -t loadgen:poc apps/loadgen
kind load docker-image bank-svc:poc loadgen:poc --name "$CLUSTER"

log "Deploying bank workload (namespace bank)"
existing=$(kubectl get deploy -n bank -o name 2>/dev/null || true)
kubectl apply -k manifests/bank
# Re-runs: restart so pods pick up a rebuilt image with the same tag.
[ -n "$existing" ] && kubectl rollout restart deploy -n bank >/dev/null
for d in auth-svc accounts-svc payments-svc cards-svc notify-svc; do
  kubectl rollout status deploy/$d -n bank --timeout=180s
done

log "Deploying load generator"
existing=$(kubectl get deploy/loadgen -n loadgen -o name 2>/dev/null || true)
kubectl apply -f manifests/loadgen/loadgen.yaml
[ -n "$existing" ] && kubectl rollout restart deploy/loadgen -n loadgen >/dev/null
kubectl rollout status deploy/loadgen -n loadgen --timeout=120s
