#!/bin/bash
# Load the dashboards into the Harvester Grafana (sidecar picks up ConfigMaps labelled
# grafana_dashboard=1 in cattle-dashboards). Idempotent. Remove with: ./apply.sh --delete
set -euo pipefail
cd "$(dirname "$0")"
NS=cattle-dashboards
for f in dashboards/*.json; do
  name="$(basename "$f" .json)"
  if [ "${1:-}" = "--delete" ]; then
    kubectl -n "$NS" delete configmap "$name" --ignore-not-found
    continue
  fi
  kubectl -n "$NS" create configmap "$name" --from-file="$name.json=$f" --dry-run=client -o yaml \
    | kubectl label --local -f - grafana_dashboard=1 app.kubernetes.io/part-of=harvester-grafana-dashboards -o yaml \
    | kubectl apply --server-side --force-conflicts -f -
done
