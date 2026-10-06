#!/usr/bin/env bash
# Create the kind cluster and register the Helm repositories (project-local Helm config).
set -euo pipefail; source scripts/lib.sh

if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  ok "kind cluster $CLUSTER already exists"
else
  log "Creating kind cluster $CLUSTER (1 control plane, 2 workers)"
  kind create cluster --name "$CLUSTER" --image "$KIND_NODE_IMAGE" --config kind/cluster.yaml --wait 120s
fi
kind export kubeconfig --name "$CLUSTER" --kubeconfig "$KUBECONFIG" >/dev/null
kubectl get nodes -o wide

log "Helm repositories"
helm repo add sail https://istio-ecosystem.github.io/sail-operator >/dev/null
helm repo add kiali https://kiali.org/helm-charts >/dev/null
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null
helm repo add grafana https://grafana.github.io/helm-charts >/dev/null
helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts >/dev/null
helm repo add jetstack https://charts.jetstack.io >/dev/null
helm repo update >/dev/null && ok "repos updated"
