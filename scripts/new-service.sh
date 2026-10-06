#!/usr/bin/env bash
# Demo: add a NEW service (loans-svc), a NEW external system (credit bureau) and a NEW
# journey (loan application) to the running POC, without touching the dashboard.
#   scripts/new-service.sh add | remove
set -euo pipefail; source scripts/lib.sh
case "${1:-add}" in
add)
  log "Images (bank-svc gains the loans route, fault-mock the credit-bureau mock, loadgen the journey)"
  docker build -q -t bank-svc:poc apps/bank-svc >/dev/null
  docker build -q -t loadgen:poc apps/loadgen >/dev/null
  docker build -q -t fault-mock:poc apps/fault-mock >/dev/null
  kind load docker-image bank-svc:poc loadgen:poc --name "$CLUSTER" >/dev/null && ok "images loaded"

  log "External system: credit-bureau container on the kind network"
  docker rm -f credit-bureau >/dev/null 2>&1 || true
  docker run -d --name credit-bureau --hostname credit-bureau --network kind --label com.bank-poc=external \
    --label com.bank-poc.dns=credit-bureau --restart unless-stopped \
    -e MOCK_NAME=credit-bureau -e PORT=8080 -e BASE_LATENCY_MS=150,400 fault-mock:poc >/dev/null && ok "credit-bureau"
  scripts/coredns-sync.sh

  log "Deploy loans-svc + gateway route + ServiceEntry (manifests/examples/loans)"
  kubectl apply -k manifests/examples/loans
  kubectl rollout status deploy/loans-svc -n bank --timeout=180s

  log "Load generator: add the loan-application journey"
  kubectl set env deploy/loadgen -n loadgen EXTRA_JOURNEYS=loan-application >/dev/null
  kubectl rollout status deploy/loadgen -n loadgen --timeout=120s
  ok "done: watch http://localhost:18088 (new nodes appear within about a minute)"
  ;;
remove)
  kubectl set env deploy/loadgen -n loadgen EXTRA_JOURNEYS- >/dev/null || true
  kubectl delete -k manifests/examples/loans --ignore-not-found
  docker rm -f credit-bureau >/dev/null 2>&1 || true
  scripts/coredns-sync.sh
  ok "removed; the nodes leave the dashboard once they drop out of the discovery window (15 min)"
  ;;
esac
