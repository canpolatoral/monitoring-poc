#!/usr/bin/env bash
# Phase 1: Sail Operator -> IstioCNI + Istio control plane -> ingress gateway.
set -euo pipefail; source scripts/lib.sh

log "Sail Operator $SAIL_CHART_VERSION"
helm_up sail-operator sail/sail-operator "$SAIL_CHART_VERSION" sail-operator --wait

log "Istio v$ISTIO_VERSION control plane + CNI"
kubectl create namespace istio-system --dry-run=client -o yaml | kubectl apply -f -
kubectl create namespace istio-cni --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f manifests/mesh/istio.yaml
wait_for istiocni/default "" condition=Ready 300s
wait_for istio/default "" condition=Ready 300s
kubectl get istio,istiocni

log "Ingress gateway"
kubectl apply -f manifests/mesh/ingress-gateway.yaml
kubectl rollout status deploy/istio-ingressgateway -n istio-ingress --timeout=180s
