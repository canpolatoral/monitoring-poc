#!/usr/bin/env bash
# Point the CoreDNS zone poc.internal at every external mock container. A container opts in
# with the label com.bank-poc.dns=<name> and becomes <name>.poc.internal.
set -euo pipefail; source scripts/lib.sh
NET=kind; hosts=""
for c in $(docker ps --filter label=com.bank-poc.dns --format '{{.Names}}'); do
  name=$(docker inspect -f '{{index .Config.Labels "com.bank-poc.dns"}}' "$c")
  ip=$(docker inspect -f "{{(index .NetworkSettings.Networks \"$NET\").IPAddress}}" "$c")
  hosts+="        $ip $name.poc.internal"$'\n'; ok "$name.poc.internal -> $ip"
done
corefile=$(kubectl get cm coredns -n kube-system -o jsonpath='{.data.Corefile}' | awk '/^poc.internal:53/{skip=1} !skip{print} skip&&/^}/{skip=0}')
corefile+=$'\n'"poc.internal:53 {"$'\n'"    errors"$'\n'"    hosts {"$'\n'"$hosts        fallthrough"$'\n'"    }"$'\n'"    cache 5"$'\n'"}"
kubectl create cm coredns -n kube-system --from-literal=Corefile="$corefile" --dry-run=client -o yaml | kubectl apply -f - >/dev/null 2>&1
kubectl rollout restart deploy/coredns -n kube-system >/dev/null && kubectl rollout status deploy/coredns -n kube-system --timeout=120s >/dev/null && ok "CoreDNS reloaded"
