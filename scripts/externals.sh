#!/usr/bin/env bash
# Phase 4: "outside the cluster" systems as Docker containers on the kind network (NOT in
# the cluster), registered in the mesh with ServiceEntries.
#   core-banking  HTTPS mock (TLS originated by the sidecar)
#   card-switch   HTTP mock
#   sms-gateway   HTTP mock, deliberately NOT registered (shows as PassthroughCluster)
#   bank-db       fault-injecting TCP proxy -> bank-db-postgres (PostgreSQL in place of Oracle)
# Names resolve through a CoreDNS "poc.internal" zone, standing in for corporate DNS.
set -euo pipefail; source scripts/lib.sh
NET=kind; LABEL=com.bank-poc=external
CERTS=$TOOLS_DIR/certs; SECRETS=$TOOLS_DIR/secrets; mkdir -p "$CERTS" "$SECRETS"

log "Throwaway CA + core-banking TLS certificate (.tools/certs, never committed)"
if [ ! -f "$CERTS/core-banking.crt" ]; then
  openssl req -x509 -newkey rsa:2048 -nodes -days 365 -subj "/CN=POC legacy CA" \
    -keyout "$CERTS/ca.key" -out "$CERTS/ca.crt" 2>/dev/null
  openssl req -newkey rsa:2048 -nodes -subj "/CN=core-banking.poc.internal" \
    -keyout "$CERTS/core-banking.key" -out "$CERTS/core-banking.csr" 2>/dev/null
  printf "subjectAltName=DNS:core-banking.poc.internal\n" > "$CERTS/ext.cnf"
  openssl x509 -req -in "$CERTS/core-banking.csr" -CA "$CERTS/ca.crt" -CAkey "$CERTS/ca.key" -CAcreateserial \
    -days 365 -extfile "$CERTS/ext.cnf" -out "$CERTS/core-banking.crt" 2>/dev/null
  chmod 644 "$CERTS/core-banking.key"   # read by UID 1000 inside the mock container
fi
ok "certificates ready"

for f in bank-db-password postgres-password; do
  [ -f "$SECRETS/$f" ] || head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' > "$SECRETS/$f"
done
DBPW=$(cat "$SECRETS/bank-db-password")

log "Building fault-mock image"
docker build -q -t fault-mock:poc apps/fault-mock >/dev/null && ok "fault-mock:poc"

run() { # name, docker args...   (DNS=<name> registers <name>.poc.internal)
  local name=$1; shift
  if docker ps -a --format '{{.Names}}' | grep -qx "$name"; then docker rm -f "$name" >/dev/null; fi
  local dns=(); [ -n "${DNS:-}" ] && dns=(--label "com.bank-poc.dns=$DNS")
  docker run -d --name "$name" --hostname "$name" --network $NET --label $LABEL "${dns[@]}" \
    --restart unless-stopped "$@" >/dev/null
  ok "$name"
}

log "Starting external systems on docker network '$NET'"
if ! docker ps --format '{{.Names}}' | grep -qx bank-db-postgres; then
  DNS= run bank-db-postgres -e POSTGRES_DB=bank -e POSTGRES_PASSWORD="$(cat "$SECRETS/postgres-password")" \
    -e BANK_RO_PASSWORD="$DBPW" -v "$PWD/externals/postgres-init.sh:/docker-entrypoint-initdb.d/init.sh:ro" postgres:17-alpine
else ok "bank-db-postgres (already running, data kept)"; fi
DNS=bank-db run bank-db      -e MOCK_NAME=bank-db -e MODE=tcp-proxy -e PORT=5432 -e UPSTREAM=bank-db-postgres:5432 -e BASE_LATENCY_MS=0,0 fault-mock:poc
DNS=core-banking run core-banking -e MOCK_NAME=core-banking -e PORT=8443 -e BASE_LATENCY_MS=80,200 \
  -e TLS_CERT=/certs/core-banking.crt -e TLS_KEY=/certs/core-banking.key -v "$CERTS:/certs:ro" fault-mock:poc
DNS=card-switch run card-switch  -e MOCK_NAME=card-switch -e PORT=8080 -e BASE_LATENCY_MS=60,150 fault-mock:poc
DNS=sms-gateway run sms-gateway  -e MOCK_NAME=sms-gateway -e PORT=8080 -e BASE_LATENCY_MS=20,60 fault-mock:poc
until docker exec bank-db-postgres pg_isready -U postgres -d bank >/dev/null 2>&1; do sleep 1; done; ok "PostgreSQL ready"

log "CoreDNS: zone poc.internal -> container IPs (stand-in for corporate DNS)"
scripts/coredns-sync.sh

log "DB credentials (Secret) and core banking CA (ConfigMap) in namespace bank"
kubectl create secret generic bank-db-credentials -n bank \
  --from-literal=dsn="postgresql://bank_ro:${DBPW}@bank-db.poc.internal:5432/bank" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl create configmap core-banking-ca -n bank --from-file=ca.crt="$CERTS/ca.crt" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
ok "applied"

log "ServiceEntries / DestinationRules / VirtualServices + app wiring"
kubectl apply -k manifests/external
kubectl apply -k manifests/bank >/dev/null
for d in accounts-svc payments-svc cards-svc notify-svc; do kubectl rollout restart deploy/$d -n bank >/dev/null; done
for d in accounts-svc payments-svc cards-svc notify-svc; do kubectl rollout status deploy/$d -n bank --timeout=180s; done
