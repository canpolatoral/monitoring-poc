# Shared helpers, sourced by the other scripts (environment comes from the Makefile).
log()  { printf '\n\033[1;35m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m  !!\033[0m %s\n' "$*"; }

# helm_up <release> <chart> <version> <namespace> [extra args...]
helm_up() {
  local rel=$1 chart=$2 ver=$3 ns=$4; shift 4
  helm upgrade --install "$rel" "$chart" --version "$ver" -n "$ns" --create-namespace "$@"
}

# wait_for <kind/name> <namespace> <condition> [timeout]
wait_for() { kubectl wait --for="$3" "$1" -n "$2" --timeout="${4:-300s}"; }

# Retry a command until it succeeds (CRDs/webhooks that are still starting).
retry() { local n=0; until "$@"; do n=$((n+1)); [ $n -ge 30 ] && return 1; sleep 5; done; }
