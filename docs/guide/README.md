# User guide

> **Not an official SUSE product; for exploration and evaluation only, provided as is.** See the
> [disclaimer](../../README.md).

How to read the `[SV+]` dashboards and alerts. The guide is separate from the Helm chart (nothing here is
installed or shipped by it). The panels shown are real, from a three-node Harvester 1.8.2 lab; VM, volume and
namespace names in the screenshots are replaced by aliases. What those numbers meant on that lab is in
[lab-readings.md](lab-readings.md), so the pages below stay about how to read a panel, not about one cluster.

## Which dashboard answers which question

| Question | Dashboard | Page |
|---|---|---|
| Is anything being starved right now, and by what (CPU, memory, disk, network)? | VM Contention | [contention.md](contention.md) |
| What is going on with this one VM? | VM Info Detail v2 | [detail.md](detail.md) |
| Which VMs are too big, too small or idle? | Right-Sizing | [rightsizing.md](rightsizing.md) |
| Can the cluster take more VMs? Lose a host? When does the disk fill? What do stopped VMs cost? | Capacity & Reclaim | [capacity.md](capacity.md) |
| Which VMs need attention, all in one table? | VM Scorecard | [scorecard.md](scorecard.md) |
| Are my VMs backed up? Where did the disk space go? | Backup & Protection | [backup.md](backup.md) |
| Who tells me when something goes wrong? | Alerts (PrometheusRule) | [alerts.md](alerts.md) |

## Suggested routines

**Daily (two minutes).** Open the **Scorecard**. The "VMs flagged" tile should be 0; if not, sort the table by
*Flags* and open the VM name to land on its Detail v2 page. Then glance at the **Contention** overview tiles: all
green means nothing is starved.

**Incident ("the VM is slow").**
1. Detail v2 for that VM: is *CPU Ready* high (host is short of CPU), is *Guest mem* high or is there swap
   (guest is short of memory), is *Latency per I/O* high (storage)?
2. If storage: compare VM disk latency with the *Longhorn* latency on Contention. Only Longhorn high means the
   storage layer (replicas, disks, network between nodes); both high with a quiet Longhorn means the guest.
3. Look at the host row (*Host this VM runs on*) and the node panels on Contention: pressure (PSI), memory
   available, disk latency and utilisation.

**Weekly / monthly.** **Right-Sizing** for what to shrink or switch off, **Capacity & Reclaim** for N-1 headroom
and the disk forecast, **Backup & Protection** for unprotected VMs and snapshot space.

## Conventions used by every dashboard

* **Variables** at the top (namespace, VM, top N, look-back window, thresholds) change what the panels count. The
  look-back window is limited by the retention of Prometheus (5 days by default on Harvester).
* **Tile colours** follow thresholds: green fine, orange attention, red act. Hover the (i) icon on any panel for
  its definition; the same text is quoted in these pages.
* **"[needs kernel PSI]"** panels stay empty until the nodes boot with `psi=1`. They exist only when the chart is
  installed with `psi.enabled=true` (see the root README).
* **Guest-agent panels** (guest memory, guest filesystems) need `qemu-guest-agent` in the VM.
* **Time-range and refresh** are the usual Grafana controls; all dashboards refresh every minute.
