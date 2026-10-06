# Harvester VM contention dashboards

Grafana dashboards for the Harvester (SUSE Virtualization) built-in `rancher-monitoring` stack that
close part of the gap to what VMware shows operators: CPU Ready, honest memory consumption, memory and
I/O pressure, storage latency. Target: **Harvester v1.8.2** (KubeVirt 1.7.4, chart `rancher-monitoring`
108.0.2+up77.9.1). Not yet run against a live cluster, see "Open items".

| Dashboard (uid) | Answers |
|---|---|
| **[SV+] Harvester VM Contention** (`harvester-vm-contention-v1`) | Which VMs/nodes are being starved right now: CPU Ready %, guest memory in use, swap/major faults, launcher OOM risk, host PSI (CPU/memory/I/O), read/write latency, network drops. Panels link to the detail dashboard. |
| **[SV+] Harvester VM Info Detail v2** (`harvester-vm-detail-v2`) | One VM end to end, plus the node it runs on. Adds what the stock `Harvester VM Info Detail` lacks. Own uid: the official `harvester-vm-*` dashboards are untouched. Both titles carry the `[SV+]` prefix and the `sv-plus` tag (`TITLE_PREFIX`/`TAG` in `generate.py`) to stand out from the stock ones. |

## Files

- `generate.py`: source of truth (`--with-psi` adds the pressure panels, see below). Writes `dashboards/*.json` and `required-metrics.json` (per-panel list of metrics).
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
| CPU Co-stop / host contention | Host CPU PSI (needs PSI, see below) | `node_pressure_cpu_waiting_seconds_total` |
| Memory Active vs Consumed | Guest memory in use | `1 - usable/available` (MemAvailable, cache not counted) |
| Swapped | Guest swap activity, major faults | `kubevirt_vmi_memory_swap_*_traffic_bytes` (gauges), `pgmajfault_total` |
| Ballooned | Memory balloon | `kubevirt_vmi_memory_actual_balloon_bytes` (normally empty on Harvester) |
| Host memory contention | Host memory PSI (needs PSI, see below), OOM kills | `node_pressure_memory_*`, `node_vmstat_oom_kill`, `container_oom_events_total` |
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

`verify-metrics.py` plus every panel query executed against the lab Prometheus: all 43 default panels
return series (or a legitimately empty result on an idle VM) with no query errors.

- `kubevirt_vmi_vcpu_delay_seconds_total` is emitted (67 series), so CPU Ready works. Guest-agent memory,
  launcher limits, CFS and OOM counters and the `node_uname_info` join on `instance` all work.
- Bug found only by running the queries: one `{__name__=~"a|b"}` selector under `rate()`/`delta()` makes
  Prometheus return HTTP 422 (identical labelsets once the name is dropped). Now summed per metric (`pair()`).
- **Stock CPU panel is wrong:** for one VM the stock v1.8.2 expression (`.../ 1000`) gives 0.0000055 where the
  underlying rate is 0.0055. The collector emits seconds, so it under-reports by 1000x.
- **PSI is off on the lab nodes**, so the pressure panels are not part of the default dashboards (next section).
- The lab was idle, so the thresholds have not been exercised under load.

## Pressure (PSI) panels: kernel prerequisite

**State.** The v1.8.2 nodes (kernel 6.12.0-160000.36-default) have PSI compiled in but switched off at boot:
`/proc/pressure` does not exist, the kernel command line has no `psi=`, node-exporter exports no
`node_pressure_*`, and every cAdvisor `container_pressure_*` series (3990, all zero) has been exactly 0 for
the whole 5-day history. (cAdvisor exports the series regardless, so their existence proves nothing.)

**Is it deliberate?** Yes, by the kernel vendor. SUSE builds with `CONFIG_PSI_DEFAULT_DISABLED=y`: the option was
added to the kernel for SUSE after a `hackbench` regression found with PSI on
([patch](https://lkml.iu.edu/hypermail/linux/kernel/1811.3/01482.html)), so users can opt in with `psi=1`. SLES 16.1
flipped the default to enabled ([release notes 7.12](https://documentation.suse.com/releasenotes/sles/html/releasenotes_sles_16.1/index.html)),
noting negligible impact for most workloads and 2-8% only on scheduling/wakeup-intensive microbenchmarks.
Harvester's OS kernel matches the SLES 16.0 behaviour; I did not find a Harvester-specific statement of intent.

**Cost of enabling.**
- Kernel: see above; expect no visible change for VM workloads, but measure if the nodes are latency-critical.
- Prometheus (lab figures: 316,841 series, ~6,400 samples/s, 1.95 bytes/sample on disk, 6.4 GB of blocks for 5d):
  host PSI adds 5 series per node (15 in total, about 40 KB/day). The cAdvisor pressure series (3,990, 1.3% of
  all series, ~66 samples/s, ~11 MB/day, ~56 MB over 5d retention) are **already ingested today** as zeros, so
  enabling PSI adds no series, only less compressible values (my estimate: a few times that, tens of MB/day).

**How to enable (not applied on the lab; the order matters).**
1. One node at a time: put it in maintenance mode from the Harvester UI.
2. On the node, per the Harvester [OS troubleshooting page](https://docs.harvesterhci.io/v1.8/troubleshooting/os/)
   (documented there as a *workaround*):
   ```
   mount -o remount,rw <COS_STATE device> /run/initramfs/cos-state     # the doc's example is /dev/vda2
   vi /run/initramfs/cos-state/grub2/grub.cfg                           # append psi=1 to the "linux (loop0)$kernel $kernelcmd" line
   reboot
   ```
   Check the result with `cat /proc/pressure/memory`. The device name is not fixed on bare metal; look for the
   partition labelled `COS_STATE`.
3. Expect to repeat it after each Harvester upgrade: a `grub.cfg` edit is reported not to survive upgrades.
   For new installs, set `os.additionalKernelArguments: "psi=1"` in the install config
   ([reference](https://docs.harvesterhci.io/v1.8/install/harvester-configuration)). I did not find a documented
   persistent way for already-installed nodes (`/oem/grubenv` and the CloudInit CRD are unverified for this).
4. For the host-wide panels also enable the collector, since node-exporter has it off by default. In the
   `rancher-monitoring` add-on `valuesContent` add under the existing `prometheus-node-exporter:` key:
   ```
   prometheus-node-exporter:
     extraArgs:
     - --collector.pressure
   ```
   (Applied and then reverted on the lab on 2026-10-06: it deploys cleanly, but with the kernel switch off it
   yields no data. Original values are in `backups/rancher-monitoring-addon-2026-10-06.yaml`.)
5. `python3 generate.py --with-psi && ./apply.sh` adds the 10 PSI panels and tiles (per-VM ones from cAdvisor
   need only step 2; host-wide ones need step 4 too).

## Open items

1. Decide whether to enable PSI (above); until then the default dashboards have no pressure panels.
2. Load test one VM (CPU and memory stress) to see CPU Ready and guest memory react.
3. Guest-agent metrics exist only for VMs running qemu-guest-agent (10 VMIs report `usable` here).

## Packaging as a Helm chart (later)

Same idea as `suse-observability-genai-dashboards`, but simpler: no apply Job or RBAC, because Grafana's
sidecar watches ConfigMaps. Planned layout: `charts/harvester-grafana-dashboards/{Chart.yaml,values.yaml,
dashboards/*.json,templates/configmap-dashboards.yaml}`, one ConfigMap per dashboard from `.Files.Glob`,
namespace `cattle-dashboards`, label `grafana_dashboard: "1"`. Keep the JSON out of `templates/` (and out of
`tpl`): legends use `{{name}}`, which Helm would try to evaluate.
