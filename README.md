# harvester-extra-dashboards

Extra Grafana dashboards and alerts for Harvester (SUSE Virtualization): VM contention, right-sizing, capacity/reclaim with a what-if, a VM scorecard, backup and snapshot protection, and 24 opt-in alert rules, packaged as a Helm chart.

Grafana dashboards for the Harvester (SUSE Virtualization) built-in `rancher-monitoring` stack that
close part of the gap to what VMware shows operators: CPU Ready, honest memory consumption, launcher OOM
risk, storage latency (including Longhorn and node disks), right-sizing and capacity (pressure/PSI panels are opt-in, see below).
Target: **Harvester v1.8.2** (KubeVirt 1.7.4, chart `rancher-monitoring` 108.0.2+up77.9.1). Verified against a Harvester v1.8.2 lab
(see "Live check"); not yet tested under a real incident.

| Dashboard (uid) | Answers |
|---|---|
| **[SV+] Harvester VM Contention** (`harvester-vm-contention-v1`) | Which VMs/nodes are being starved right now: CPU Ready %, guest memory in use, swap/major faults, launcher OOM risk, read/write latency, network drops (plus per-VM and host PSI panels in the `psi` variant). Panels link to the detail dashboard. |
| **[SV+] Harvester VM Info Detail v2** (`harvester-vm-detail-v2`) | One VM end to end, plus the node it runs on. Adds what the stock `Harvester VM Info Detail` lacks. Own uid: the official `harvester-vm-*` dashboards are untouched. Both titles carry the `[SV+]` prefix and the `sv-plus` tag (`TITLE_PREFIX`/`TAG` in `generate.py`) to stand out from the stock ones. |
| **[SV+] Harvester Right-Sizing** (`harvester-rightsizing-v1`) | Which running VMs are idle, over- or under-provisioned. Tunable thresholds and look-back window (variables). Per-VM p95 CPU/memory against what was provisioned, a **suggested** vCPU/memory size at a target utilisation, and the total vCPU/memory that could be reclaimed. |
| **[SV+] Harvester Capacity & Reclaim** (`harvester-capacity-v1`) | Cluster level: vCPU:core ratio, memory allocated/requested, **N-1 headroom**, per-host overcommit and utilisation, stopped VMs and the disk they hold, Longhorn thin-provisioning and node storage, detached volumes, PVCs that no VM or pod uses, large mostly-empty guest filesystems, and a **what-if**: how many more VMs of a profile you define still fit by memory (now and after losing the biggest host), CPU, Longhorn scheduling and real disk space, which resource is the limit, and a linear days-until-disk-full forecast. |
| **[SV+] Harvester VM Scorecard** (`harvester-vm-scorecard-v1`) | One sortable row per running VM: CPU used/Ready, guest and launcher memory, VM-level and Longhorn write latency, IOPS, drops, unhealthy volumes, volumes never backed up, and the right-sizing savings. **Flags** counts the warning thresholds a VM is over; the VM name opens the detail dashboard. |
| **[SV+] Harvester Backup & Protection** (`harvester-backup-v1`) | Which VMs are protected by Longhorn backups: fully protected, never backed up, stale (threshold is a variable), the size of the data that has never been backed up, backups in error, plus the space held by snapshots (user-created vs Longhorn's own), backup storage over time, and a per-VM table with newest/oldest backup age and snapshot space. |

**How to read every panel, with screenshots: [docs/guide](docs/guide/README.md)** (one page per dashboard and for the alerts, plus [example readings from a lab](docs/guide/lab-readings.md)).

## Install

Needs the Harvester `rancher-monitoring` add-on enabled (Grafana's sidecar loads ConfigMaps labelled
`grafana_dashboard=1` from `cattle-dashboards`). The chart only creates those ConfigMaps (and, if you opt in with `alerts.enabled`, one PrometheusRule): no hooks, RBAC or images.

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
| `dashboards.contention.enabled`, `.detail.enabled`, `.rightsizing.enabled`, `.capacity.enabled`, `.scorecard.enabled`, `.backup.enabled` | `true` | Install each dashboard |
| `alerts.enabled` | `false` | Install the PrometheusRule with 24 alerts (see "Alerts") |
| `alerts.thresholds.*`, `alerts.namespace`, `alerts.labels` | see `values.yaml` | Alert thresholds, rule namespace, rule labels |
| `dashboardsNamespace` | `cattle-dashboards` | Namespace Grafana's sidecar watches |
| `sidecar.label` / `sidecar.labelValue` | `grafana_dashboard` / `"1"` | Sidecar selector |
| `labels`, `annotations` | `{}` | Extra metadata on the ConfigMaps (e.g. a sidecar folder annotation) |

ConfigMaps are named `<release>-vm-contention`, `<release>-vm-detail-v2`, `<release>-rightsizing`, `<release>-capacity`, `<release>-scorecard` and `<release>-backup` (plus the PrometheusRule `<release>-alerts` when enabled). Do not mix the chart with
`apply.sh` on the same cluster: both define the same dashboard uids (`./apply.sh --delete` first).

## Files

- `charts/harvester-extra-dashboards/`: the Helm chart. `dashboards/` and `dashboards-psi/` hold the generated JSON of the two variants, `alerts/` the generated alert rules (JSON).
- `generate.py`: **source of truth** for the dashboards and the alert rules. Writes both variants and the rules into the chart and `required-metrics.json` (per-panel list of metrics; PSI-only panels are flagged). Do not edit the generated JSON by hand.
- `validate.py`: offline checks of both variants (JSON, uids, variables, every dashboard and alert PromQL expression parses, PSI variant is a superset, every alert token is handled by the template). `pip install promql-parser`.
- `tests/chart_check.py`: renders the chart in several configurations and asserts the ConfigMap content is byte-identical to the source JSON, and that the alerts render with substituted thresholds and untouched `{{ $labels }}` templates. `pip install pyyaml`; needs `helm`.
- `verify-metrics.py`: run against a live Prometheus; reports which panels lack metrics, plus the label/join assumptions.
- `verify-queries.py`: run against a live Prometheus; executes every dashboard query the way Grafana does (6 h range for graphs, instant for tiles and tables) and every alert expression, and fails on any error. It exists because `validate.py` (syntax) and instant queries both missed a `found duplicate series` error that only shows in a long range after a kube-state-metrics restart.
- `apply.sh`: Helm-less alternative that loads the dashboard JSON as ConfigMaps (not the alerts); `--psi` selects the PSI variant, `--delete` removes them.
- `docs/guide/`: user guide (overview, one page per dashboard, alerts, example lab readings) with screenshots in `docs/guide/images/`. Not part of the chart.
- `docs/alertmanager/`: how to email the alerts (a tested Alertmanager route and receiver). The default Harvester Alertmanager sends alerts nowhere.
- `docs/mailpit/`: the lab's mail sink (Mailpit with persistent storage, two random logins and an Ingress) that makes the alert emails readable in a browser; manifest, install, operations and troubleshooting.

```
python3 generate.py && python3 validate.py && python3 tests/chart_check.py && helm lint charts/harvester-extra-dashboards
kubectl -n cattle-monitoring-system port-forward svc/rancher-monitoring-prometheus 9090 &
python3 verify-metrics.py http://localhost:9090
python3 verify-queries.py http://localhost:9090
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

## Alerts

`alerts.enabled=true` installs one `PrometheusRule` (`<release>-alerts`) with 24 rules in four groups. It is **off by
default** because it starts firing in Alertmanager, whose routes decide who is notified (the Harvester Prometheus
selects rules from every namespace, so no extra wiring is needed to see them in the Prometheus and Alertmanager UIs).
Thresholds are values (`alerts.thresholds.*`); the rule text is never templated, so `{{ $labels.name }}` reaches
Prometheus intact. **A default Harvester Alertmanager has only a `null` receiver, so nobody is notified**: see
[`docs/alertmanager/`](docs/alertmanager/README.md) for a tested email route and [`docs/mailpit/`](docs/mailpit/README.md) for a mail sink you can read in a browser.

| Group | Alerts |
|---|---|
| `harvester-extra.vm-contention` | CPU Ready high (warning 5%, critical 10%), guest memory high, launcher memory near its limit, launcher OOM-killed, VM disk write latency, Longhorn write latency of a VM's volumes, VM network drops |
| `harvester-extra.host-contention` | node OOM kills, host memory / CPU / I/O pressure (PSI, silent until `psi=1`), node disk latency, node disk saturated |
| `harvester-extra.capacity-and-storage` | cluster cannot lose its biggest host (N-1 memory), node memory requests high, Longhorn scheduling high / full, Longhorn node disks filling, Longhorn volume degraded / faulted |
| `harvester-extra.backups-and-snapshots` | a backed-up volume whose last backup is older than `backupMaxAgeDays` (default 7), a Longhorn backup in state Error, snapshots holding more than `snapshotSpacePercent` (default 50) of the used Longhorn disk. Volumes that were *never* backed up are deliberately not alerted on (the lab has 40 of 50): see the dashboard |

Each expression was parsed, then evaluated against the lab Prometheus: all 24 run without error, and with the
thresholds forced to 0 every rule that has data returns series (the event rules, such as OOM kills and degraded
volumes, were checked by confirming their selectors exist). On the lab only **`HarvesterMemoryN1Exceeded`** fires
today (145%). Longhorn exposes `robustness` as one series per state with a 0/1 value, and detached volumes report
`unknown`, so only `degraded` and `faulted` alert.

## Storage contention (VM Contention dashboard)

Under "Storage and network contention": Longhorn read/write latency and IOPS **per VM** (Longhorn's own measurement,
mapped from the VM's PVCs; the unit is nanoseconds and is converted to ms), node disk latency (await) and utilisation
of the worst physical disk per node, and a table of degraded/faulted volumes (empty is good). Read them against the
VM-level latency above: slow at the VM and at Longhorn means the storage layer; slow only at the VM points at the
guest or the virtual disk path.

## What-if and backup: how to read them

**What-if (Capacity & Reclaim).** Define a VM in the variables (vCPUs, memory, disk, replicas, expected fill %) and the
dashboard counts how many more of them fit. It is an estimate built from the scheduler's and Longhorn's own rules, not a
promise:
- *Memory* is the scheduler's view: (allocatable - requested) / (VM memory / memory overcommit + QEMU overhead). Set
  "Harvester memory overcommit" to the `overcommit-config` memory value (the lab uses 175%, the default is 150%). The
  N-1 variant removes the largest host first; 0 means a host failure could not be absorbed.
- *CPU* is not limited by requests (Harvester overcommits CPU 16x by default) but by what VMs really use:
  (target % x host cores - host CPU p95 over the window) / (VM vCPUs x the average CPU use per vCPU of today's VMs).
- *Longhorn scheduling* is (usable disk x over-provisioning % - scheduled) / (disk x replicas); *real disk space*
  keeps the minimal-available reserve and assumes new disks fill to the expected fill %.
- *Disk full in (days)* extrapolates the growth of Longhorn used space linearly over the window (at most 5 days of data)
  for the cluster **as it is today**: it does not depend on the what-if VM. Treat a restore or a large import inside the
  window as a spike, not a trend. *Disk full (+N VMs)* is the same forecast after taking the space of "VMs to add now"
  (disk x replicas x fill %) out of the free space first; with 0 VMs the two tiles are equal, and 0 days means the new VMs
  alone would fill the disk. On the lab: 20.8 days today, 8.0 days after adding 20 VMs of 100 GiB.

**Backup & Protection.** `longhorn_volume_last_backup_at` is the epoch of the last backup (0 = never) and
`longhorn_backup_state` is 0=New, 1=Pending, 2=InProgress, 3=Completed, 4=Error. VMs are mapped to volumes through the
PVCs of their disks, so stopped VMs are included. **Snapshots are not backups**: they sit on the same disks, and
Longhorn does export their sizes (`longhorn_snapshot_actual_size_bytes`, `user_created` true/false). On the lab 94
user-created snapshots hold 1.3 TiB, about 40% of the used Longhorn disk.

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

## Live check (lab, Harvester v1.8.2, 2026-10-06 and 2026-10-07)

`verify-metrics.py` plus every panel and alert query executed against the lab Prometheus: all 87 default panels (97 in
the PSI variant) and all 21 alert expressions run without query errors, and all 65 metrics they use exist. The dashboards
were then opened in Grafana (through the Harvester API proxy) and the problems that only show there were fixed.

- `kubevirt_vmi_vcpu_delay_seconds_total` is emitted, so CPU Ready works. Guest-agent memory, launcher limits, CFS and
  OOM counters, Longhorn per-volume metrics and the `node_uname_info` join on `instance` all work.
- Found only by running the queries: one `{__name__=~"a|b"}` selector under `rate()`/`delta()` makes Prometheus return
  HTTP 422 (identical labelsets once the name is dropped), now summed per metric (`pair()`); duplicate
  `node_uname_info` series over a long window do the same, now collapsed with `max by (instance, nodename)`.
- Found only by looking at Grafana: tables built from raw series showed stray label columns and pushed the values out of
  view; the node-disk await formula gave +Inf on idle disks (also in an alert); tile titles were truncated.
- **Stock CPU panel is wrong:** for one VM the stock v1.8.2 expression (`.../ 1000`) gives 0.0000055 where the
  underlying rate is 0.0055. The collector emits seconds, so it under-reports by 1000x.
- Longhorn latency metrics are in **nanoseconds**; `longhorn_volume_state` and `longhorn_volume_robustness` are one
  series per state (label `state`, value 0/1) and detached volumes report robustness `unknown`.
- What the lab showed: CPU Ready peaked near 17% while VMs were bunched on fewer hosts during node reboots, the busiest
  disk of one node sat at 100% for stretches (await up to ~250 ms), memory requests are 145% of what would remain after
  losing the biggest host, 40 of 50 Longhorn volumes have never been backed up, and `harv01lab` had 20 kernel OOM kills.
- PSI is now enabled on all three lab nodes (next section) and its panels show data.

## Pressure (PSI) panels: kernel prerequisite

**State before enabling (2026-10-06).** The v1.8.2 nodes (kernel 6.12.0-160000.36-default) have PSI compiled in but switched off at boot:
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

**How to enable (this is how it was enabled on the three lab nodes on 2026-10-07; the order matters).**

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

**Verified on one node first (2026-10-06).** After setting `psi=1` on `harvlab-witness` only: `node_pressure_*` (5 series)
and non-zero cAdvisor pressure appeared for that node within minutes, with no node-exporter change, while the other
two nodes stayed at exactly 0. With the PSI variant installed the host panels showed that node (CPU "some" 2.2%,
I/O "some" 38.7% / "full" 31.9%, memory 0); the per-VM panels stay at 0 until the nodes running the VMs are switched.

**Result on all three nodes (2026-10-07).** After the reboots, `node_pressure_*` is exported for every node and the
cAdvisor per-VM pressure series are non-zero, again without touching node-exporter. The "Max VM memory/CPU PSI" tiles and
the per-VM and host pressure panels show real values, and the three host pressure alerts are live.

## Open items

1. Load test one VM (CPU and memory stress) to see CPU Ready, PSI and guest memory react under a real incident; the lab
   has only been idle or rebooting, so no alert threshold has been tuned against a genuine problem.
2. Right-sizing conclusions rest on 5 days of Prometheus data and on qemu-guest-agent for memory; see "Right-sizing and
   capacity: how to read them".
3. Alertmanager: the default config notifies nobody. `docs/alertmanager/` is applied on the lab (mail catcher, behind
   its own login and a Cloudflare-tunnelled Ingress), but the Alertmanager config Secret may be reset by an add-on
   redeploy (untested), and the catcher page is protected by a password only (a Cloudflare Access policy is advisable).
4. Gaps against VMware's tools that are not built: showback by namespace, a DRS-like balance and migration view,
   longer metric retention (an add-on change), and a cost model for "reclaimable". An earlier version of this file
   said Harvester exports no snapshot metrics; that was wrong, Longhorn does and the Backup & Protection dashboard uses them.
5. The what-if is only as good as its inputs: the memory-overcommit variable has to be set by hand to match your
   `overcommit-config`, and the CPU estimate assumes new VMs behave like today's average.

## Releasing

`.github/workflows/release.yaml` runs [chart-releaser](https://github.com/helm/chart-releaser-action) on pushes
that touch `charts/**` (same approach as `suse-observability-genai-dashboards`): it creates a GitHub release with
the packaged chart and updates the Helm repository index on the `gh-pages` branch. Bump `version` in `Chart.yaml`
for every chart change (`skip_existing` ignores versions already released). Prerequisites: a `gh-pages` branch must
exist and GitHub Pages must serve it; for the `helm repo add` URL to work without credentials the repository has to
be public.
