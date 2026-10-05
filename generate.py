#!/usr/bin/env python3
"""Generate the Harvester Grafana dashboards (JSON) and the per-panel metric manifest.

Targets Harvester v1.8.2 = KubeVirt 1.7.4 + rancher-monitoring (kube-prometheus-stack).
Metric names/units were checked against the KubeVirt v1.7.4 source (see README.md).

    python3 generate.py          # writes dashboards/*.json and required-metrics.json
"""
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "dashboards")

DS = {"type": "prometheus", "uid": "${datasource}"}
VM_LINK = {
    "title": "Open VM detail",
    "url": "/d/harvester-vm-detail-v2?var-namespace=${__field.labels.namespace}"
           "&var-vm=${__field.labels.name}&${__url_time_range}",
}

# Label filter used on every KubeVirt (virt-handler) series.
F = '{namespace=~"$namespace",name=~"$vm",node=~"$node"}'
# Series of the virt-launcher pod of the selected VMs (cAdvisor / kube-state-metrics).
LP = 'namespace=~"$namespace",container="compute",pod=~"virt-launcher-$vm-[a-z0-9]{5}"'
# Map a node-exporter series (instance) to the Kubernetes node name, filtered by $node.
NJ = '* on (instance) group_left (nodename) node_uname_info{nodename=~"$node"}'
# vCPU count without relying on KubeVirt recording rules (one series per vCPU id and state).
VCPUS = ('count by (namespace, name) (group by (namespace, name, id) '
         '(kubevirt_vmi_vcpu_seconds_total%s))')


def vcpus(flt=F):
    return VCPUS % flt


def cpu_ready(flt=F):
    return ('100 * sum by (namespace, name) (rate(kubevirt_vmi_vcpu_delay_seconds_total%s[5m])) / %s'
            % (flt, vcpus(flt)))


def cpu_usage(flt=F):
    return ('100 * sum by (namespace, name) (rate(kubevirt_vmi_cpu_usage_seconds_total%s[5m])) / %s'
            % (flt, vcpus(flt)))


def guest_mem_pct(flt=F):
    return ('100 * (1 - max by (namespace, name) (kubevirt_vmi_memory_usable_bytes%s) '
            '/ max by (namespace, name) (kubevirt_vmi_memory_available_bytes%s))' % (flt, flt))


def launcher_name(expr):
    return 'label_replace(%s, "name", "$1", "pod", "virt-launcher-(.*)-[a-z0-9]{5}")' % expr


def launcher_ws_pct():
    return launcher_name(
        '100 * container_memory_working_set_bytes{%s} '
        '/ on (namespace, pod, container) kube_pod_container_resource_limits{resource="memory",%s}' % (LP, LP))


def steps(*pairs):
    return [{"color": c, "value": v} for c, v in pairs]


OK_WARN_BAD = lambda w, b: steps(("green", None), ("orange", w), ("red", b))  # noqa: E731


class Dash:
    def __init__(self, uid, title, description, tags, variables, links=None):
        self.d = {
            "uid": uid, "title": title, "description": description, "tags": tags,
            "schemaVersion": 39, "version": 1, "editable": True, "graphTooltip": 1,
            "refresh": "1m", "time": {"from": "now-6h", "to": "now"},
            "timezone": "", "links": links or [],
            "annotations": {"list": []},
            "templating": {"list": variables}, "panels": [],
        }
        self.y = 0
        self.x = 0
        self.rowh = 0
        self.id = 0
        self.manifest = []

    def _next_id(self):
        self.id += 1
        return self.id

    def row(self, title):
        self.y += self.rowh
        self.x = self.rowh = 0
        self.d["panels"].append({"type": "row", "title": title, "collapsed": False, "id": self._next_id(),
                                 "gridPos": {"h": 1, "w": 24, "x": 0, "y": self.y}, "panels": []})
        self.y += 1

    def _place(self, w, h):
        if self.x + w > 24:
            self.y += self.rowh
            self.x = self.rowh = 0
        pos = {"h": h, "w": w, "x": self.x, "y": self.y}
        self.x += w
        self.rowh = max(self.rowh, h)
        return pos

    def _record(self, title, desc, targets):
        metrics = sorted({m for t in targets for m in re.findall(
            r"\b((?:kubevirt|node|container|kube)_[a-z0-9_]+)", t[0])})
        self.manifest.append({"dashboard": self.d["uid"], "panel": title, "metrics": metrics})

    def ts(self, title, desc, targets, unit="short", w=8, h=8, minv=0, maxv=None, thr=None,
           link=False, stack=False, fill=10, legend=True):
        self._record(title, desc, targets)
        defaults = {
            "unit": unit, "min": minv,
            "custom": {"lineWidth": 1, "fillOpacity": fill, "showPoints": "never", "spanNulls": False,
                       "stacking": {"mode": "normal" if stack else "none"},
                       "thresholdsStyle": {"mode": "dashed" if thr else "off"}},
            "thresholds": {"mode": "absolute", "steps": thr or steps(("green", None))},
        }
        if maxv is not None:
            defaults["max"] = maxv
        if link:
            defaults["links"] = [VM_LINK]
        self.d["panels"].append({
            "type": "timeseries", "title": title, "description": desc, "datasource": DS,
            "id": self._next_id(), "gridPos": self._place(w, h),
            "targets": [{"refId": chr(65 + i), "datasource": DS, "expr": e, "legendFormat": l,
                         "range": True, "instant": False} for i, (e, l) in enumerate(targets)],
            "fieldConfig": {"defaults": defaults, "overrides": []},
            "options": {"legend": {"displayMode": "table" if legend else "hidden", "placement": "bottom",
                                   "calcs": ["mean", "max"]},
                        "tooltip": {"mode": "multi", "sort": "desc"}},
        })

    def stat(self, title, desc, expr, unit="short", w=4, h=4, thr=None, text="auto", legend=""):
        self._record(title, desc, [(expr, legend)])
        self.d["panels"].append({
            "type": "stat", "title": title, "description": desc, "datasource": DS,
            "id": self._next_id(), "gridPos": self._place(w, h),
            "targets": [{"refId": "A", "datasource": DS, "expr": expr, "legendFormat": legend,
                         "range": False, "instant": True}],
            "fieldConfig": {"defaults": {"unit": unit, "noValue": "n/a",
                                         "thresholds": {"mode": "absolute",
                                                        "steps": thr or steps(("blue", None))}},
                            "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"], "values": False},
                        "colorMode": "background" if thr else "value", "graphMode": "none",
                        "textMode": text, "justifyMode": "center"},
        })


def var_ds():
    return {"type": "datasource", "name": "datasource", "label": "Data source", "query": "prometheus",
            "current": {}, "hide": 0}


def var_query(name, label, query, multi=True, all_value=None):
    v = {"type": "query", "name": name, "label": label, "datasource": DS,
         "definition": query, "query": {"query": query, "refId": name}, "refresh": 2, "sort": 1,
         "multi": multi, "includeAll": multi, "hide": 0}
    if multi:
        v["allValue"] = all_value
        v["current"] = {"selected": True, "text": ["All"], "value": ["$__all"]}
    return v


def var_topn():
    return {"type": "custom", "name": "topn", "label": "Top N", "query": "5,10,20,50", "hide": 0,
            "current": {"selected": True, "text": "10", "value": "10"},
            "options": [{"selected": n == "10", "text": n, "value": n} for n in ("5", "10", "20", "50")]}


def var_hidden_node():
    # node of the selected VM, used by the host row of the detail dashboard
    return {"type": "query", "name": "node", "label": "Node", "datasource": DS, "hide": 2, "refresh": 2,
            "definition": 'label_values(kubevirt_vmi_info{namespace="$namespace",name="$vm"}, node)',
            "query": {"query": 'label_values(kubevirt_vmi_info{namespace="$namespace",name="$vm"}, node)',
                      "refId": "node"},
            "multi": False, "includeAll": False}


# --------------------------------------------------------------------------- contention
def contention():
    d = Dash(
        "harvester-vm-contention-v1", "Harvester VM Contention",
        "Where are VMs being starved? CPU run-queue delay (vSphere CPU Ready analogue), guest and host memory "
        "pressure, storage latency and network drops. Harvester 1.8.x / KubeVirt 1.7.x.",
        ["harvester", "kubevirt", "contention"],
        [var_ds(),
         var_query("node", "Node", "label_values(kubevirt_vmi_info, node)", all_value=".*"),
         var_query("namespace", "Namespace", 'label_values(kubevirt_vmi_info{node=~"$node"}, namespace)',
                   all_value=".*"),
         var_query("vm", "VM", 'label_values(kubevirt_vmi_info{namespace=~"$namespace",node=~"$node"}, name)',
                   all_value=".+"),
         var_topn()],
        links=[{"title": "VM detail", "type": "link", "url": "/d/harvester-vm-detail-v2",
                "icon": "external link", "targetBlank": False}])

    d.row("Overview: is anything being starved right now?")
    d.stat("VMs with CPU Ready > 5%", "VMs whose vCPUs spend more than 5% of the time runnable but not "
           "scheduled. vSphere guidance: >5% per vCPU is a warning, >10% is a problem.",
           "count((%s) > 5) or vector(0)" % cpu_ready(), thr=OK_WARN_BAD(1, 5))
    d.stat("Worst CPU Ready", "Highest CPU Ready % of any VM in the selection.",
           "max(%s)" % cpu_ready(), unit="percent", thr=OK_WARN_BAD(5, 10))
    d.stat("VMs with guest memory > 90%", "VMs whose guest OS reports less than 10% memory available "
           "(MemAvailable, so page cache is NOT counted as used). Needs qemu-guest-agent.",
           "count((%s) > 90) or vector(0)" % guest_mem_pct(), thr=OK_WARN_BAD(1, 3))
    d.stat("Max host memory PSI (some)", "Share of the last 5 minutes in which at least one task on the "
           "node stalled waiting for memory (kernel PSI). Any sustained value means host memory contention.",
           "max(100 * rate(node_pressure_memory_waiting_seconds_total[5m]) %s)" % NJ, unit="percent",
           thr=OK_WARN_BAD(1, 10))
    d.stat("Max host CPU PSI (some)", "Share of the last 5 minutes in which runnable tasks on the node "
           "waited for a CPU (kernel PSI).",
           "max(100 * rate(node_pressure_cpu_waiting_seconds_total[5m]) %s)" % NJ, unit="percent",
           thr=OK_WARN_BAD(10, 25))
    d.stat("OOM kills (1h)", "OOM kills on the nodes plus OOM events of virt-launcher compute containers "
           "in the last hour. A launcher OOM kill takes the VM down.",
           "sum(increase(node_vmstat_oom_kill[1h]) %s) + "
           "(sum(increase(container_oom_events_total{%s}[1h])) or vector(0))" % (NJ, LP),
           thr=steps(("green", None), ("red", 1)))

    d.row("CPU contention")
    d.ts("CPU Ready % per VM (top N)", "Time vCPUs spent runnable but waiting for a physical CPU, as % of "
         "wall time, averaged over the VM's vCPUs (kubevirt_vmi_vcpu_delay_seconds_total). Equivalent of "
         "vSphere CPU Ready %. Needs kernel schedstats; no series means the node does not expose it.",
         [("topk($topn, %s)" % cpu_ready(), "{{name}}")], unit="percent", thr=OK_WARN_BAD(5, 10),
         link=True, w=12)
    d.ts("CPU usage % of allocated vCPUs (top N)", "Guest + hypervisor CPU time as % of the VM's vCPUs "
         "(kubevirt_vmi_cpu_usage_seconds_total). Read together with CPU Ready: high ready with low usage "
         "means the host, not the guest, is the bottleneck.",
         [("topk($topn, %s)" % cpu_usage(), "{{name}}")], unit="percent", link=True, w=12)
    d.ts("vCPU I/O wait per VM (top N)", "Time vCPUs spent blocked waiting on I/O, per vCPU "
         "(kubevirt_vmi_vcpu_wait_seconds_total). Not CPU steal: this points at storage.",
         [("topk($topn, 100 * sum by (namespace, name) (rate(kubevirt_vmi_vcpu_wait_seconds_total%s[5m])) / %s)"
           % (F, vcpus()), "{{name}}")], unit="percent", link=True)
    d.ts("Host CPU pressure (PSI) per node", "Kernel pressure-stall information: % of time tasks waited "
         "for CPU. 'some' = at least one task stalled.",
         [("100 * rate(node_pressure_cpu_waiting_seconds_total[5m]) %s" % NJ, "{{nodename}}")],
         unit="percent", thr=OK_WARN_BAD(10, 25))
    d.ts("virt-launcher CFS throttling (top N)", "% of scheduling periods in which the VM's launcher "
         "container hit its CPU limit and was throttled. Relevant when CPU overcommit sets limits.",
         [("topk($topn, %s)" % launcher_name(
             '100 * sum by (namespace, pod) (rate(container_cpu_cfs_throttled_periods_total{%s}[5m])) '
             '/ sum by (namespace, pod) (rate(container_cpu_cfs_periods_total{%s}[5m]))' % (LP, LP)),
           "{{name}}")], unit="percent", thr=OK_WARN_BAD(5, 25), link=True)

    d.row("Memory contention")
    d.ts("Guest memory in use % (top N)", "100 * (1 - MemAvailable / total), from the guest agent. Unlike "
         "the stock Harvester panel (available - unused) this does not count reclaimable page cache as used. "
         "Closest to vSphere 'Active' memory.",
         [("topk($topn, %s)" % guest_mem_pct(), "{{name}}")], unit="percent", maxv=100,
         thr=OK_WARN_BAD(80, 95), link=True)
    d.ts("Guest swap activity (top N)", "Change of the guest's swap-in + swap-out counters over 5 minutes "
         "(kubevirt_vmi_memory_swap_*_traffic_bytes are gauges). Any sustained value means the guest is "
         "short of memory. Equivalent of vSphere 'Swapped' but measured inside the guest.",
         [("topk($topn, sum by (namespace, name) (clamp_min(delta({__name__=~\"kubevirt_vmi_memory_swap_(in|out)"
           "_traffic_bytes\",namespace=~\"$namespace\",name=~\"$vm\",node=~\"$node\"}[5m]), 0)))", "{{name}}")],
         unit="bytes", link=True)
    d.ts("Guest major page faults/s (top N)", "Page faults that needed disk I/O "
         "(kubevirt_vmi_memory_pgmajfault_total). Rising values mean the guest is thrashing.",
         [("topk($topn, sum by (namespace, name) (rate(kubevirt_vmi_memory_pgmajfault_total%s[5m])))" % F,
           "{{name}}")], unit="ops", link=True)
    d.ts("Memory balloon (non-zero VMs)", "Balloon size per VM (kubevirt_vmi_memory_actual_balloon_bytes). "
         "Harvester does not reclaim through the balloon by default, so empty is normal; vSphere 'Ballooned' "
         "has no routine equivalent here.",
         [("kubevirt_vmi_memory_actual_balloon_bytes%s > 0" % F, "{{name}}")], unit="bytes", link=True)
    d.ts("virt-launcher memory vs limit % (top N)", "Working set of the VM's launcher container as % of its "
         "memory limit. Near 100% means the launcher (guest RAM + QEMU overhead) is about to be OOM-killed.",
         [("topk($topn, %s)" % launcher_ws_pct(), "{{name}}")], unit="percent", maxv=110,
         thr=OK_WARN_BAD(90, 98), link=True)
    d.ts("Host memory available %", "MemAvailable / MemTotal per node.",
         [("(100 * node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) %s" % NJ, "{{nodename}}")],
         unit="percent", maxv=100, thr=steps(("red", None), ("orange", 10), ("green", 20)))
    d.ts("Host memory pressure (PSI) per node", "'some': at least one task waited for memory; 'full': all "
         "non-idle tasks stalled. This is the host-side memory contention signal.",
         [("100 * rate(node_pressure_memory_waiting_seconds_total[5m]) %s" % NJ, "{{nodename}} some"),
          ("100 * rate(node_pressure_memory_stalled_seconds_total[5m]) %s" % NJ, "{{nodename}} full")],
         unit="percent", thr=OK_WARN_BAD(1, 10))
    d.ts("Host major faults/s and OOM kills", "node_vmstat_pgmajfault rate and OOM kills per node.",
         [("rate(node_vmstat_pgmajfault[5m]) %s" % NJ, "{{nodename}} major faults/s"),
          ("increase(node_vmstat_oom_kill[5m]) %s" % NJ, "{{nodename}} OOM kills")], unit="short")

    d.row("Storage and network contention")
    d.ts("Read latency per VM disk (top N)", "Average time per read I/O: rate(read_times_seconds) / "
         "rate(iops_read). Equivalent of vSphere virtual-disk read latency.",
         [("topk($topn, 1000 * rate(kubevirt_vmi_storage_read_times_seconds_total%s[5m]) "
           "/ (rate(kubevirt_vmi_storage_iops_read_total%s[5m]) > 0))" % (F, F), "{{name}} {{drive}}")],
         unit="ms", thr=OK_WARN_BAD(20, 50), link=True)
    d.ts("Write latency per VM disk (top N)", "Average time per write I/O. Compare with Longhorn volume "
         "latency to see which layer is slow.",
         [("topk($topn, 1000 * rate(kubevirt_vmi_storage_write_times_seconds_total%s[5m]) "
           "/ (rate(kubevirt_vmi_storage_iops_write_total%s[5m]) > 0))" % (F, F), "{{name}} {{drive}}")],
         unit="ms", thr=OK_WARN_BAD(20, 50), link=True)
    d.ts("Host I/O pressure (PSI) per node", "% of time tasks waited for block I/O on the node.",
         [("100 * rate(node_pressure_io_waiting_seconds_total[5m]) %s" % NJ, "{{nodename}} some"),
          ("100 * rate(node_pressure_io_stalled_seconds_total[5m]) %s" % NJ, "{{nodename}} full")],
         unit="percent", thr=OK_WARN_BAD(10, 30))
    d.ts("Network drops/s per VM (top N)", "vNIC rx + tx dropped packets.",
         [("topk($topn, sum by (namespace, name) (rate({__name__=~\"kubevirt_vmi_network_(receive|transmit)"
           "_packets_dropped_total\",namespace=~\"$namespace\",name=~\"$vm\",node=~\"$node\"}[5m])))",
           "{{name}}")], unit="pps", link=True, w=12)
    d.ts("Network errors/s per VM (top N)", "vNIC rx + tx errors.",
         [("topk($topn, sum by (namespace, name) (rate({__name__=~\"kubevirt_vmi_network_(receive|transmit)"
           "_errors_total\",namespace=~\"$namespace\",name=~\"$vm\",node=~\"$node\"}[5m])))",
           "{{name}}")], unit="pps", link=True, w=12)
    return d


# --------------------------------------------------------------------------- detail v2
def detail():
    one = '{namespace="$namespace",name="$vm"}'
    lp1 = 'namespace="$namespace",container="compute",pod=~"virt-launcher-$vm-[a-z0-9]{5}"'
    d = Dash(
        "harvester-vm-detail-v2", "Harvester VM Info Detail v2",
        "Single-VM view that complements the stock 'Harvester VM Info Detail': adds CPU Ready, honest "
        "guest memory (page cache not counted as used), launcher OOM risk, I/O latency and host pressure.",
        ["harvester", "kubevirt", "vm"],
        [var_ds(),
         var_query("namespace", "Namespace", "label_values(kubevirt_vmi_info, namespace)", multi=False),
         var_query("vm", "VM", 'label_values(kubevirt_vmi_info{namespace="$namespace"}, name)', multi=False),
         var_hidden_node()],
        links=[{"title": "Contention overview", "type": "link", "url": "/d/harvester-vm-contention-v1",
                "icon": "external link", "targetBlank": False}])
    # single-select defaults must not be "All"
    for v in d.d["templating"]["list"]:
        if v.get("multi") is False:
            v.pop("current", None)

    d.row("Summary")
    d.stat("State", "Phase and node of the VM.", "kubevirt_vmi_info%s" % one, text="name", w=6,
           legend="{{phase}} on {{node}}")
    d.stat("vCPUs", "Number of vCPUs.", vcpus(one), w=3)
    d.stat("Memory", "Memory allocated to the domain.", "max(kubevirt_vmi_memory_domain_bytes%s)" % one,
           unit="bytes", w=3)
    d.stat("CPU Ready", "See the CPU Ready panel below.", cpu_ready(one), unit="percent", w=4,
           thr=OK_WARN_BAD(5, 10))
    d.stat("Guest memory in use", "100 * (1 - MemAvailable / total). Needs qemu-guest-agent.",
           guest_mem_pct(one), unit="percent", w=4, thr=OK_WARN_BAD(80, 95))
    d.stat("Launcher mem vs limit", "virt-launcher working set / memory limit.",
           launcher_ws_pct().replace('namespace=~"$namespace"', 'namespace="$namespace"'),
           unit="percent", w=4, thr=OK_WARN_BAD(90, 98))

    d.row("CPU")
    d.ts("CPU usage % of vCPUs", "kubevirt_vmi_cpu_usage_seconds_total (guest + hypervisor) / vCPUs.",
         [(cpu_usage(one), "usage")], unit="percent", maxv=100, w=6)
    d.ts("CPU Ready %", "Run-queue delay per vCPU (kubevirt_vmi_vcpu_delay_seconds_total). vSphere CPU "
         "Ready analogue.", [(cpu_ready(one), "ready")], unit="percent", thr=OK_WARN_BAD(5, 10), w=6)
    d.ts("vCPU I/O wait %", "Time vCPUs were blocked on I/O.",
         [("100 * sum by (namespace, name) (rate(kubevirt_vmi_vcpu_wait_seconds_total%s[5m])) / %s"
           % (one, vcpus(one)), "io wait")], unit="percent", w=6)
    d.ts("Launcher CFS throttling %", "Share of periods the launcher hit its CPU limit.",
         [('100 * sum(rate(container_cpu_cfs_throttled_periods_total{%s}[5m])) '
           '/ sum(rate(container_cpu_cfs_periods_total{%s}[5m]))' % (lp1, lp1), "throttled")],
         unit="percent", thr=OK_WARN_BAD(5, 25), w=6)

    d.row("Memory")
    d.ts("Guest memory breakdown", "available = total seen by guest; usable = MemAvailable (free + "
         "reclaimable); unused = completely free; cached = page cache. In use = available - usable.",
         [("max(kubevirt_vmi_memory_available_bytes%s)" % one, "total"),
          ("max(kubevirt_vmi_memory_available_bytes%s) - max(kubevirt_vmi_memory_usable_bytes%s)" % (one, one),
           "in use"),
          ("max(kubevirt_vmi_memory_cached_bytes%s)" % one, "page cache"),
          ("max(kubevirt_vmi_memory_unused_bytes%s)" % one, "free")], unit="bytes", w=8, fill=0)
    d.ts("Guest swap and major faults", "Swap traffic delta (gauge counters) and major page faults/s.",
         [("sum(clamp_min(delta({__name__=~\"kubevirt_vmi_memory_swap_(in|out)_traffic_bytes\","
           "namespace=\"$namespace\",name=\"$vm\"}[5m]), 0))", "swap bytes / 5m"),
          ("sum(rate(kubevirt_vmi_memory_pgmajfault_total%s[5m]))" % one, "major faults/s")],
         unit="short", w=8)
    d.ts("Launcher working set vs request/limit", "Host-side memory of the launcher pod against what it is "
         "allowed. Working set approaching the limit = OOM risk.",
         [("sum(container_memory_working_set_bytes{%s})" % lp1, "working set"),
          ('sum(kube_pod_container_resource_requests{resource="memory",%s})' % lp1, "request"),
          ('sum(kube_pod_container_resource_limits{resource="memory",%s})' % lp1, "limit"),
          ("max(kubevirt_vmi_memory_resident_bytes%s)" % one, "QEMU resident (RSS)"),
          ("max(kubevirt_vmi_launcher_memory_overhead_bytes%s)" % one, "launcher overhead")],
         unit="bytes", w=8, fill=0)

    d.row("Storage")
    d.ts("IOPS", "Read/write operations per second.",
         [("sum(rate(kubevirt_vmi_storage_iops_read_total%s[5m]))" % one, "read"),
          ("sum(rate(kubevirt_vmi_storage_iops_write_total%s[5m]))" % one, "write")], unit="iops", w=6)
    d.ts("Throughput", "Read/write bytes per second.",
         [("sum(rate(kubevirt_vmi_storage_read_traffic_bytes_total%s[5m]))" % one, "read"),
          ("sum(rate(kubevirt_vmi_storage_write_traffic_bytes_total%s[5m]))" % one, "write")],
         unit="Bps", w=6)
    d.ts("Latency per I/O", "rate(times) / rate(ops). The stock dashboard's 'IO Time' shows the raw rate of "
         "the time counter, which is not a latency.",
         [("1000 * sum(rate(kubevirt_vmi_storage_read_times_seconds_total%s[5m])) / "
           "(sum(rate(kubevirt_vmi_storage_iops_read_total%s[5m])) > 0)" % (one, one), "read"),
          ("1000 * sum(rate(kubevirt_vmi_storage_write_times_seconds_total%s[5m])) / "
           "(sum(rate(kubevirt_vmi_storage_iops_write_total%s[5m])) > 0)" % (one, one), "write"),
          ("1000 * sum(rate(kubevirt_vmi_storage_flush_times_seconds_total%s[5m])) / "
           "(sum(rate(kubevirt_vmi_storage_flush_requests_total%s[5m])) > 0)" % (one, one), "flush")],
         unit="ms", thr=OK_WARN_BAD(20, 50), w=6)
    d.ts("Guest filesystem used %", "From the guest agent (kubevirt_vmi_filesystem_*).",
         [("100 * max by (mount_point) (kubevirt_vmi_filesystem_used_bytes%s) / "
           "max by (mount_point) (kubevirt_vmi_filesystem_capacity_bytes%s)" % (one, one), "{{mount_point}}")],
         unit="percent", maxv=100, thr=OK_WARN_BAD(80, 95), w=6)

    d.row("Network")
    d.ts("Traffic", "Bits per second per interface.",
         [("8 * sum by (interface) (rate(kubevirt_vmi_network_receive_bytes_total%s[5m]))" % one, "rx {{interface}}"),
          ("8 * sum by (interface) (rate(kubevirt_vmi_network_transmit_bytes_total%s[5m]))" % one,
           "tx {{interface}}")], unit="bps", w=8)
    d.ts("Dropped packets/s", "rx + tx drops.",
         [("sum by (interface) (rate({__name__=~\"kubevirt_vmi_network_(receive|transmit)_packets_dropped_total\","
           "namespace=\"$namespace\",name=\"$vm\"}[5m]))", "{{interface}}")], unit="pps", w=8)
    d.ts("Errors/s", "rx + tx errors.",
         [("sum by (interface) (rate({__name__=~\"kubevirt_vmi_network_(receive|transmit)_errors_total\","
           "namespace=\"$namespace\",name=\"$vm\"}[5m]))", "{{interface}}")], unit="pps", w=8)

    d.row("Host this VM runs on")
    nj1 = '* on (instance) group_left (nodename) node_uname_info{nodename="$node"}'
    d.ts("Node pressure (PSI)", "CPU, memory and I/O pressure of the hosting node ('some').",
         [("100 * rate(node_pressure_cpu_waiting_seconds_total[5m]) %s" % nj1, "cpu"),
          ("100 * rate(node_pressure_memory_waiting_seconds_total[5m]) %s" % nj1, "memory"),
          ("100 * rate(node_pressure_io_waiting_seconds_total[5m]) %s" % nj1, "io")],
         unit="percent", thr=OK_WARN_BAD(10, 25), w=12)
    d.ts("Node memory available %", "MemAvailable / MemTotal of the hosting node.",
         [("(100 * node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) %s" % nj1, "available")],
         unit="percent", maxv=100, thr=steps(("red", None), ("orange", 10), ("green", 20)), w=12)
    return d


def main():
    os.makedirs(OUT, exist_ok=True)
    manifest = []
    for name, dash in (("harvester-vm-contention", contention()), ("harvester-vm-detail-v2", detail())):
        with open(os.path.join(OUT, name + ".json"), "w") as f:
            json.dump(dash.d, f, indent=2)
            f.write("\n")
        manifest += dash.manifest
    with open(os.path.join(HERE, "required-metrics.json"), "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    print("wrote %d panels" % len(manifest))


if __name__ == "__main__":
    main()
