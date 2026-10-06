#!/bin/bash
# Load the dashboards into the Harvester Grafana WITHOUT Helm (sidecar picks up ConfigMaps labelled
# grafana_dashboard=1 in cattle-dashboards). Idempotent.
#   ./apply.sh            default dashboards (no PSI panels)
#   ./apply.sh --psi      variant with the pressure (PSI) panels
#   ./apply.sh --delete   remove them
# Do not mix with the Helm chart: both define the same dashboard uids.
set -euo pipefail
cd "$(dirname "$0")/charts/harvester-extra-dashboards"
NS=cattle-dashboards
DIR=dashboards
[ "${1:-}" = "--psi" ] && DIR=dashboards-psi
for f in "$DIR"/*.json; do
  name="$(basename "$f" .json)"
  if [ "${1:-}" = "--delete" ]; then
    kubectl -n "$NS" delete configmap "$name" --ignore-not-found
    continue
  fi
  kubectl -n "$NS" create configmap "$name" --from-file="$name.json=$f" --dry-run=client -o yaml \
    | kubectl label --local -f - grafana_dashboard=1 app.kubernetes.io/part-of=harvester-extra-dashboards -o yaml \
    | kubectl apply --server-side --force-conflicts -f -
done
