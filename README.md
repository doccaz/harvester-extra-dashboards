# Harvester VM contention dashboards

Grafana dashboards for the Harvester (SUSE Virtualization) built-in `rancher-monitoring` stack that
close part of the gap to what VMware shows operators: CPU Ready, honest memory consumption, memory and
I/O pressure, storage latency. Target: **Harvester v1.8.2** (KubeVirt 1.7.4, chart `rancher-monitoring`
108.0.2+up77.9.1). Not yet run against a live cluster, see "Open items".

| Dashboard (uid) | Answers |
|---|---|
| **Harvester VM Contention** (`harvester-vm-contention-v1`) | Which VMs/nodes are being starved right now: CPU Ready %, guest memory in use, swap/major faults, launcher OOM risk, host PSI (CPU/memory/I/O), read/write latency, network drops. Panels link to the detail dashboard. |
| **Harvester VM Info Detail v2** (`harvester-vm-detail-v2`) | One VM end to end, plus the node it runs on. Adds what the stock `Harvester VM Info Detail` lacks. Own uid: the official `harvester-vm-*` dashboards are untouched. |

## Files

- `generate.py`: source of truth. Writes `dashboards/*.json` and `required-metrics.json` (per-panel list of metrics).
- `validate.py`: offline checks (JSON, uids, variables, every PromQL expression parses). `pip install promql-parser`.
- `verify-metrics.py`: **run when the lab is reachable.** Reports which panels lack metrics, plus the label/join assumptions.
- `apply.sh`: loads the JSON as ConfigMaps (`grafana_dashboard=1`) in `cattle-dashboards`; `--delete` removes them.

```
python3 generate.py && python3 validate.py
kubectl -n cattle-monitoring-system port-forward svc/rancher-monitoring-prometheus 9090 &
python3 verify-metrics.py http://localhost:9090
./apply.sh
```

## vSphere to Harvester mapping used

| vSphere | Panel | Metric |
|---|---|---|
| CPU Ready % | CPU Ready % | `rate(kubevirt_vmi_vcpu_delay_seconds_total)` / vCPUs |
| CPU Co-stop / host contention | Host CPU PSI | `node_pressure_cpu_waiting_seconds_total` |
| Memory Active vs Consumed | Guest memory in use | `1 - usable/available` (MemAvailable, cache not counted) |
| Swapped | Guest swap activity, major faults | `kubevirt_vmi_memory_swap_*_traffic_bytes` (gauges), `pgmajfault_total` |
| Ballooned | Memory balloon | `kubevirt_vmi_memory_actual_balloon_bytes` (normally empty on Harvester) |
| Host memory contention | Host memory PSI, OOM kills | `node_pressure_memory_*`, `node_vmstat_oom_kill`, `container_oom_events_total` |
| Virtual disk latency | Read/write latency | `rate(*_times_seconds_total) / rate(iops_*)` |

## Facts checked in source (KubeVirt v1.7.4, Harvester v1.8.2)

- Time counters are converted to **seconds** by the collector (`nanosecondsToSeconds`).
- `kubevirt_vmi_vcpu_delay_seconds_total` = run-queue wait (the CPU Ready analogue); `vcpu_wait_seconds_total` is **I/O** wait.
- `swap_in/out_traffic_bytes` are **gauges**, hence `delta()` instead of `rate()`.
- `kubevirt_vmi_vcpu_count`, `_memory_used_bytes`, `_phase_count`, `_guest_vcpu_queue` and the
  `kubevirt_vm_container_memory_request_margin_*` metrics are **recording rules**, not raw series. They are
  not used here, so the panels do not depend on KubeVirt's PrometheusRule being deployed.
- node-exporter's default `vmstat` fields do not include `pgscan_*`; only `oom_kill`, `pgmajfault`, `pswp*` are used.
- Stock Harvester dashboards (installer v1.8.2) have no contention panel; their Memory panel uses
  `available - unused`, which counts page cache as used, and `IO Time` plots the raw time counter, not a latency.
- Prometheus in the add-on: scrape 1m, retention 5d / 50 GiB. Hence `[5m]` windows and no multi-week panels.

## Open items (need the live cluster)

1. Run `verify-metrics.py`; panels listed as MISSING need either a different metric or removal.
2. Confirm PSI is on (`node_pressure_*`) and that cAdvisor exposes `container_oom_events_total`.
3. Confirm `kubevirt_vmi_vcpu_delay_seconds_total` is emitted (needs kernel schedstats) and that
   `node_uname_info` joins on `instance`.
4. The v1.8.2 installer's stock CPU panel divides `vcpu_seconds_total` by 1000 although the collector emits
   seconds: check whether the stock panel under-reports CPU by 1000x (the script prints peak usage % as a sanity check).
5. Guest-agent metrics (`usable`, filesystems) exist only for VMs running qemu-guest-agent.

## Packaging as a Helm chart (later)

Same idea as `suse-observability-genai-dashboards`, but simpler: no apply Job or RBAC, because Grafana's
sidecar watches ConfigMaps. Planned layout: `charts/harvester-grafana-dashboards/{Chart.yaml,values.yaml,
dashboards/*.json,templates/configmap-dashboards.yaml}`, one ConfigMap per dashboard from `.Files.Glob`,
namespace `cattle-dashboards`, label `grafana_dashboard: "1"`. Keep the JSON out of `templates/` (and out of
`tpl`): legends use `{{name}}`, which Helm would try to evaluate.
