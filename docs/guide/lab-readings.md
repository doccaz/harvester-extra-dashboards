# Example readings from a lab

What the dashboards showed on one real, small cluster, and what we concluded. It is an *example of how to read
them*, not a benchmark: three Harvester 1.8.2 nodes (two hosts plus a witness), 24 VMs of which 12 were running, snapshot
taken on 2026-10-07 around noon with the chart at 0.4.5 and the look-back window at 3 days. Names are aliased
(`node-a`, `node-b`, `witness`, `cache-01`, `api-01`, ...), the numbers are real.

[Back to the overview](README.md)

## At a glance ([Scorecard](scorecard.md))

12 VMs running, 2 flagged, 0 unhealthy volumes, 23 volumes never backed up.

| VM | Why it was flagged | Reading |
|---|---|---|
| `cache-01` | Longhorn write latency 43 ms (above 20 ms) and VM-side disk write 41 ms | Both layers agree, so the disk path is slow, not just the guest. Correlates with disk latency spikes on a node (below) |
| `api-01` | Guest memory 84.5% and launcher at **97.7% of its memory limit** | The launcher is close to being OOM-killed. Also the one under-sized VM on Right-Sizing (memory p95 137%) |

## Contention ([Contention](contention.md))

* **CPU Ready**: worst VM 0.15%, no VM above 5%. CPU is not scarce here even though vCPUs outnumber cores
  (1.36 : 1): the VMs are mostly idle.
* **Memory**: one VM near its limit (`api-01`). No OOM kills in the last hour, balloon "No data" (normal).
* **Disks**: the worst device on `node-a` peaked at about 340 ms and on `node-b` at about 175 ms await; the busiest
  device on `node-b` reached 91% utilisation. One of the disks of `node-b` also
  reset its I/O counters several times in a few minutes, which is what made the utilisation panel report more
  than 100% before 0.4.3 clamped it. A flapping disk is the likely cause: check `dmesg` and SMART on that host.
* **I/O pressure**: the witness node showed 30-38% I/O pressure (*some*) all day, while hosting no VMs. It runs
  etcd, whose constant small synchronous writes are sensitive to disk speed: worth knowing before putting a
  witness on a slow disk.
* **Network**: two VMs drop about 6 packets/s steadily, under the 10/s alert level and flat, so a baseline rather
  than an event.

## Right-sizing ([Right-Sizing](rightsizing.md))

* 1 idle candidate (`test-01`: CPU p95 3.4%, 0.9 IOPS, 6.6 kbit/s network).
* **11 of 12 running VMs are oversized**: roughly **51 vCPUs and 11 GiB** reclaimable. Typical row: 8 vCPUs
  provisioned, CPU p95 15%, suggestion 2 vCPUs. The fleet graph shows about 70 vCPUs provisioned against about 5
  cores used.
* The suggested memory is *higher* than allocated for some VMs; the saving column shows 0 B there, because the
  recommendation only subtracts. Read those as "do not shrink", not as "grow".
* Caveat that applies to every number above: three days is a short window.

## Capacity ([Capacity & Reclaim](capacity.md))

* vCPU : core 1.36, memory allocated 43%, but **memory requested 76% and N-1 headroom 152%**: with the largest host
  gone the remaining one cannot hold all requests. This is the cluster's real constraint and it is also what fires
  `HarvesterMemoryN1Exceeded`. CPU is not the limit.
* **What-if** for a 4 vCPU / 8 GiB / 100 GiB VM with 2 replicas: 8 more fit by memory today, **0 after losing a
  host**, 57 by CPU, 50 by Longhorn scheduling, 33 by real disk. The tile says *memory (N-1) 0*: memory, not CPU or
  disk, is the limit.
* **Disk full in about 14 days** at the growth of the last three days.
  Either the growth is a one-off (an import, restores) or the disk needs attention: check the forecast again after a
  quiet week.
* **Stopped VMs hold storage**: 12 stopped VMs, 1.16 TiB provisioned, 229 GiB actually used. Four have been stopped
  for more than 30 days (one for 137 days).
* Disk scheduled is 81%; Longhorn refuses new volumes beyond its over-provisioning limit, so the scheduling room
  (50 more VMs) is far from the real constraint.

## Backups ([Backup & Protection](backup.md))

* 24 VMs have disks; **5 fully protected, 18 not protected** (1.27 TiB of data never backed up), 2 stale, 0 errors.
* **Snapshots hold 1.41 TiB, 41% of the used Longhorn space**, with 92 user snapshots. They are not backups and
  live on the same disks; the largest consumers are a handful of volumes with 150-240 GiB of snapshots each.
* One VM's newest backup is 1.5 days old and its oldest 11.5 days, and it also has a never-backed-up volume: the
  schedule seems to cover only some of its disks. `HarvesterBackupStale` was pending or firing for two volumes.

## What we did with it

| Finding | Next step |
|---|---|
| Memory N-1 above 100% | Free memory (right-size the oversized VMs: the 11 GiB found above is a start) or add a host |
| `api-01` near its limit | Increase its memory; it is the only under-sized VM |
| Slow disk on `node-b` | Check `dmesg` / SMART, replace if it keeps resetting |
| 18 VMs unprotected | Decide which are disposable and schedule backups for the rest |
| Snapshots at 41% | Prune old snapshots of the largest consumers once their owners agree |
| Stopped VMs, 1.16 TiB | Review the four stopped for more than 30 days |
