#!/usr/bin/env bash
# Inject or clear faults in the external mocks. Usage:
#   scripts/fault.sh core-slow | db-down | switch-down | reset | status
#   scripts/fault.sh set <mock> key=value...   (mock: core-banking, card-switch, sms-gateway, bank-db)
set -euo pipefail; source scripts/lib.sh
ctl() { docker exec "$1" python faultctl.py "${@:2}"; }
case "${1:-status}" in
  core-slow)   ctl core-banking set latency_ms=2500 jitter_ms=1500 ;;   # 1-4 s; ~1/3 exceed the 3 s timeout (UT)
  db-down)     ctl bank-db set mode=down ;;                              # connection refused (UF)
  switch-down) ctl card-switch set mode=down ;;                          # UF, then UH after ejection
  reset)       for m in core-banking card-switch sms-gateway bank-db; do ctl $m reset; done ;;
  set)         ctl "$2" set "${@:3}" ;;
  status)      for m in core-banking card-switch sms-gateway bank-db; do ctl $m get; done ;;
  *) echo "unknown: $1"; exit 1 ;;
esac
