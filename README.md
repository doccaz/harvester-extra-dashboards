# harvester-extra-dashboards

Extra Grafana dashboards for Harvester (SUSE Virtualization): VM contention, right-sizing and capacity/reclaim.

Grafana dashboards for the Harvester (SUSE Virtualization) built-in `rancher-monitoring` stack that
close part of the gap to what VMware shows operators: CPU Ready, honest memory consumption, launcher OOM
risk, storage latency (pressure/PSI panels are opt-in, see below). Target: **Harvester v1.8.2** (KubeVirt 1.7.4, chart `rancher-monitoring`
108.0.2+up77.9.1). Verified against a Harvester v1.8.2 lab (see "Live check"); not yet tested under load.

| Dashboard (uid) | Answers |
|---|---|
| **[SV+] Harvester VM Contention** (`harvester-vm-contention-v1`) | Which VMs/nodes are being starved right now: CPU Ready %, guest memory in use, swap/major faults, launcher OOM risk, read/write latency, network drops (plus per-VM and host PSI panels in the `psi` variant). Panels link to the detail dashboard. |
| **[SV+] Harvester VM Info Detail v2** (`harvester-vm-detail-v2`) | One VM end to end, plus the node it runs on. Adds what the stock `Harvester VM Info Detail` lacks. Own uid: the official `harvester-vm-*` dashboards are untouched. Both titles carry the `[SV+]` prefix and the `sv-plus` tag (`TITLE_PREFIX`/`TAG` in `generate.py`) to stand out from the stock ones. |
| **[SV+] Harvester Right-Sizing** (`harvester-rightsizing-v1`) | Which running VMs are idle, over- or under-provisioned. Tunable thresholds and look-back window (variables). Per-VM p95 CPU/memory against what was provisioned, a **suggested** vCPU/memory size at a target utilisation, and the total vCPU/memory that could be reclaimed. |
| **[SV+] Harvester Capacity & Reclaim** (`harvester-capacity-v1`) | Cluster level: vCPU:core ratio, memory allocated/requested, **N-1 headroom**, per-host overcommit and utilisation, stopped VMs and the disk they hold, Longhorn thin-provisioning and node storage, detached volumes, PVCs that no VM or pod uses, large mostly-empty guest filesystems. |

## Install

Needs the Harvester `rancher-monitoring` add-on enabled (Grafana's sidecar loads ConfigMaps labelled
`grafana_dashboard=1` from `cattle-dashboards`). The chart only creates those ConfigMaps, no hooks, RBAC or images.

```bash
# from a clone of this repo
helm install harvester-extra-dashboards charts/harvester-extra-dashboards \
  --namespace cattle-monitoring-system

# once the chart is released (see "Releasing")
helm repo add harvester-extra-dashboards https://doccaz.github.io/harvester-extra-dashboards/
helm install harvester-extra-dashboards harvester-extra-dashboards/harvester-extra-dashboards -n cattle-monitoring-system
```

Grafana loads them within about a minute: look for the `[SV+]` dashboards, or filter by the tag `sv-plus`.

| Value | Default | |
|---|---|---|
| `psi.enabled` | `false` | Install the variant with the pressure (PSI) panels; needs kernel PSI, see below |
| `dashboards.contention.enabled`, `.detail.enabled`, `.rightsizing.enabled`, `.capacity.enabled` | `true` | Install each dashboard |
| `dashboardsNamespace` | `cattle-dashboards` | Namespace Grafana's sidecar watches |
| `sidecar.label` / `sidecar.labelValue` | `grafana_dashboard` / `"1"` | Sidecar selector |
| `labels`, `annotations` | `{}` | Extra metadata on the ConfigMaps (e.g. a sidecar folder annotation) |

ConfigMaps are named `<release>-vm-contention`, `<release>-vm-detail-v2`, `<release>-rightsizing` and `<release>-capacity`. Do not mix the chart with
`apply.sh` on the same cluster: both define the same dashboard uids (`./apply.sh --delete` first).

## Files

- `charts/harvester-extra-dashboards/`: the Helm chart. `dashboards/` and `dashboards-psi/` hold the generated JSON of the two variants.
- `generate.py`: **source of truth.** Writes both variants into the chart and `required-metrics.json` (per-panel list of metrics; PSI-only panels are flagged). Do not edit the JSON by hand.
- `validate.py`: offline checks of both variants (JSON, uids, variables, every PromQL expression parses, PSI variant is a superset). `pip install promql-parser`.
- `tests/chart_check.py`: renders the chart in several configurations and asserts the ConfigMap content is byte-identical to the source JSON. `pip install pyyaml`; needs `helm`.
- `verify-metrics.py`: run against a live Prometheus; reports which panels lack metrics, plus the label/join assumptions.
- `apply.sh`: Helm-less alternative that loads the JSON as ConfigMaps; `--psi` selects the PSI variant, `--delete` removes them.

```
python3 generate.py && python3 validate.py && python3 tests/chart_check.py && helm lint charts/harvester-extra-dashboards
kubectl -n cattle-monitoring-system port-forward svc/rancher-monitoring-prometheus 9090 &
python3 verify-metrics.py http://localhost:9090
```

CI (`.github/workflows/ci.yaml`) runs the same checks and fails if the committed JSON differs from what
`generate.py` produces.

## Right-sizing and capacity: how to read them

Both dashboards are for **decisions**, not alerts: they list candidates and a suggested size, a person confirms.

- **Look-back window** (`1d`/`3d`/`5d`, default `3d`) drives every p95/average. The Harvester add-on keeps
  only **5 days** of Prometheus data (`retention: 5d`, 50 GiB), so a monthly batch job or a quarter-end peak is
  invisible and a VM can look idle or oversized when it is not. Treat 5 days as a first filter. For 14-30 days,
  raise `retention`/`retentionSize` and the PVC of the add-on's Prometheus (lab figures: 316,841 series, 6.4 GB of
  blocks for 5 days, about 1.3 GB/day; 14 days is about 18 GB, 30 days about 39 GB, which does not fit the default
  50 GiB PVC with headroom).
- **Suggested size** = p95 usage / target utilisation (default 70%), rounded up to whole vCPUs and whole GiB.
  "Reclaimable" is provisioned minus suggested, never negative. The **peak** column shows the spikes p95 hides.
- **Idle candidate** = CPU p95, average network and average disk IOPS all below their thresholds for the whole
  window. **Under-sized** = CPU p95 or guest memory p95 above the thresholds; check CPU Ready on the contention
  dashboard before adding vCPUs.
- **Memory** columns need qemu-guest-agent (they use available minus MemAvailable, so reclaimable page cache does not
  count as used). VMs without it show blanks, not zeros.
- A VM **resized inside the window** is judged against its new size using usage measured at the old one (e.g. a VM
  shrunk from 12 to 8 GiB can show memory p95 above 100%). Shorten the window or wait for history at the new size.
- **Running** means a current VMI exists, so VMs that were stopped inside the window do not distort the totals.
- **Stopped VMs** come from `kubevirt_vm_info{status_group="non_running"}`. The "last transition" timestamp metric
  exists for every VM (0 for running or unknown), so it is only used for the days-stopped column.
- **PVCs used by neither a VM nor a pod** excludes system namespaces and image-backed volumes; it still only
  lists *candidates*: a PVC can be bound for a purpose the metrics cannot see.
- **N-1 headroom** = pod memory requests / (allocatable memory minus the largest host); hosts exclude the witness
  (label `node-role.harvesterhci.io/witness`). Above 100% the cluster cannot reschedule everything if that host fails.
- Tables join their columns with Grafana's `merge` transformation on namespace/VM, so each column query is
  restricted to the same VM set; the VM name links to the detail dashboard.

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

Harvester has a supported, upgrade-safe place for extra kernel arguments: the GRUB variable
`third_party_kernel_args` in `/oem/grubenv` (the persistent OEM partition). The v1.8.2 `bootargs.cfg` appends
`$third_party_kernel_args` to the kernel command line, the install-time option `os.additionalKernelArguments`
writes the same variable, and the [v1.6 to v1.7 upgrade notes](https://docs.harvesterhci.io/v1.7/upgrade/v1-6-x-to-v1-7-x/)
use exactly this mechanism (for `ifname=` arguments). The upgrade script (`upgrade_node.sh`) reads the current
value and only appends `multipath=off` when absent, so the setting is carried over on upgrades. (That last point
comes from the code; the docs do not state it explicitly.)

1. One node at a time: put it in maintenance mode from the Harvester UI.
2. On the node (SSH). `set` **replaces the whole variable**, and real nodes carry arguments you must not lose
   (NIC-name pins such as `ifname=eno1:aa:bb:..`, `pcie_acs_override=`, `multipath=off`, `consoleblank=`).
   So list first, then append `psi=1` to what is there:
   ```
   sudo grub2-editenv /oem/grubenv list            # look at it; keep a copy: sudo cat /oem/grubenv > ~/grubenv.bak
   cur=$(sudo grub2-editenv /oem/grubenv list | sed -n 's/^third_party_kernel_args=//p')
   case " $cur " in
     *" psi="*) echo "psi already set: $cur" ;;
     *)         sudo grub2-editenv /oem/grubenv set third_party_kernel_args="$cur psi=1" ;;
   esac
   sudo grub2-editenv /oem/grubenv list            # must show every original argument plus psi=1
   sudo reboot
   ```
   Do not paste a literal `set third_party_kernel_args="psi=1"` or `"multipath=off psi=1"`: on the lab nodes that
   would have dropped the `ifname=` pins (and, on one, `pcie_acs_override`). After the reboot check that the NIC
   names are unchanged, `tr ' ' '\n' < /proc/cmdline | grep psi`, and `ls /proc/pressure` (cpu io memory).
3. Take the node out of maintenance mode, wait for it to be healthy, then repeat on the next node.
   For new installs, set `os.additionalKernelArguments: "psi=1"` in the install config
   ([reference](https://docs.harvesterhci.io/v1.8/install/harvester-configuration)); it ends up in the same variable.
   The older recipe of editing `grub.cfg` on `COS_STATE` (Harvester's
   [OS troubleshooting page](https://docs.harvesterhci.io/v1.8/troubleshooting/os/), labelled a workaround) is not
   needed for this.
4. Nothing to change in node-exporter. Its pressure collector is on by default and simply exports nothing while
   `/proc/pressure` is missing; `node_pressure_*` appears as soon as a node boots with `psi=1`. (An earlier
   version of this README said the collector had to be enabled in the add-on; that was wrong, see "Verified".)
5. `helm upgrade harvester-extra-dashboards charts/harvester-extra-dashboards -n cattle-monitoring-system --set psi.enabled=true`
   (or `./apply.sh --psi`) adds the 10 PSI panels and tiles. Each panel only has data for the nodes that already
   run with `psi=1`.

**Verified on one node (2026-10-06).** After setting `psi=1` on `harvlab-witness` only: `node_pressure_*` (5 series)
and non-zero cAdvisor pressure appeared for that node within minutes, with no node-exporter change, while the other
two nodes stayed at exactly 0. With the PSI variant installed the host panels showed that node (CPU "some" 2.2%,
I/O "some" 38.7% / "full" 31.9%, memory 0); the per-VM panels stay at 0 until the nodes running the VMs are switched.

## Open items

1. Decide whether to enable PSI (above); until then the default dashboards have no pressure panels.
2. Load test one VM (CPU and memory stress) to see CPU Ready and guest memory react.
3. Guest-agent metrics exist only for VMs running qemu-guest-agent (10 VMIs report `usable` here).
4. Right-sizing and capacity dashboards: every query was run against the lab Prometheus (the tables' merged
   columns were checked row by row, including with loosened thresholds), but the Grafana rendering of the tables
   (column order, cell colours, sorting, VM links) was checked in Grafana on 2026-10-07 and fixed (stray label columns, truncated titles); re-check after upgrading.
5. Their conclusions rest on 5 days of data; see "Right-sizing and capacity: how to read them".

## Releasing

`.github/workflows/release.yaml` runs [chart-releaser](https://github.com/helm/chart-releaser-action) on pushes
that touch `charts/**` (same approach as `suse-observability-genai-dashboards`): it creates a GitHub release with
the packaged chart and updates the Helm repository index on the `gh-pages` branch. Bump `version` in `Chart.yaml`
for every chart change (`skip_existing` ignores versions already released). Prerequisites: a `gh-pages` branch must
exist and GitHub Pages must serve it; for the `helm repo add` URL to work without credentials the repository has to
be public.
