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

## Live check (lab, Harvester v1.8.2, 2026-10-06)

`verify-metrics.py` plus every panel query executed against the lab Prometheus: all return series or an
empty result without errors, except the host-PSI panels below.

- **Host PSI is absent.** node-exporter 1.9.1 runs without the pressure collector (off by default; the
  add-on does not pass `--collector.pressure`), so no `node_pressure_*` series exist. The four host-PSI
  panels are titled `[needs node-exporter pressure collector]` and stay empty until it is enabled
  (`prometheus-node-exporter.extraArgs: [--collector.pressure]` in the `rancher-monitoring` add-on values;
  not applied, it is a cluster change).
- **Per-VM PSI works instead.** cAdvisor exposes `container_pressure_{cpu,memory,io}_*` for the
  `virt-launcher` compute containers (kernel PSI is on), used for the "VM ... pressure (PSI)" panels and
  overview tiles. There is no root-cgroup pressure, so no host-wide figure from cAdvisor.
- `kubevirt_vmi_vcpu_delay_seconds_total` is emitted (67 series), so CPU Ready works.
- Guest-agent memory, launcher limits, CFS and OOM counters, and `node_uname_info` joins on `instance` all work.
- Fixed a bug the syntax check could not see: one `{__name__=~"a|b"}` selector under `rate()`/`delta()` makes
  Prometheus return HTTP 422 (identical labelsets once the name is dropped). Now summed per metric (`pair()`).
- **Stock CPU panel confirmed wrong:** for one VM the stock v1.8.2 expression (`.../ 1000`) gives 0.0000055
  where the underlying rate is 0.0055. The collector emits seconds, so it under-reports by 1000x.
- Lab was idle (PSI and CPU Ready near zero), so the contention thresholds are not yet exercised under load.

## Open items

1. Decide whether to enable the node-exporter pressure collector, then check the host-PSI panels.
2. Load test one VM (CPU and memory stress) to see CPU Ready, PSI and guest memory panels react.
3. Guest-agent metrics exist only for VMs running qemu-guest-agent (10 VMIs report `usable` here).

## Packaging as a Helm chart (later)

Same idea as `suse-observability-genai-dashboards`, but simpler: no apply Job or RBAC, because Grafana's
sidecar watches ConfigMaps. Planned layout: `charts/harvester-grafana-dashboards/{Chart.yaml,values.yaml,
dashboards/*.json,templates/configmap-dashboards.yaml}`, one ConfigMap per dashboard from `.Files.Glob`,
namespace `cattle-dashboards`, label `grafana_dashboard: "1"`. Keep the JSON out of `templates/` (and out of
`tpl`): legends use `{{name}}`, which Helm would try to evaluate.
