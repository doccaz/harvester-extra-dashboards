#!/usr/bin/env python3
"""Generate the Harvester Grafana dashboards (JSON) and the per-panel metric manifest.

Targets Harvester v1.8.2 = KubeVirt 1.7.4 + rancher-monitoring (kube-prometheus-stack).
Metric names/units were checked against the KubeVirt v1.7.4 source (see README.md).

    python3 generate.py   # writes charts/harvester-extra-dashboards/{dashboards,dashboards-psi}/*.json
                          # and required-metrics.json
"""
import json
import os
import re

# Pressure (PSI) panels need kernel PSI enabled (SUSE kernels boot with it off unless psi=1; see README).
# Two variants are generated: "dashboards" (no PSI panels, so no always-zero panels on stock Harvester) and
# "dashboards-psi"; the Helm chart picks one with `psi.enabled`.
WITH_PSI = False

# Makes our dashboards stand out in the Grafana list next to the stock Harvester/Kubernetes ones.
TITLE_PREFIX = "[SV+] "
TAG = "sv-plus"

HERE = os.path.dirname(os.path.abspath(__file__))
CHART = os.path.join(HERE, "charts", "harvester-extra-dashboards")
OUT = {False: os.path.join(CHART, "dashboards"), True: os.path.join(CHART, "dashboards-psi")}

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
NJ = '* on (instance) group_left (nodename) max by (instance, nodename) (node_uname_info{nodename=~"$node"})'
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


def expand_alternation(pattern):
    """'a_(x|y)_b' -> ['a_x_b', 'a_y_b'] (single group, as used in __name__ selectors)."""
    m = re.search(r"\(([A-Za-z0-9_|]+)\)", pattern)
    if not m:
        return [pattern]
    return [pattern[:m.start()] + alt + pattern[m.end():] for alt in m.group(1).split("|")]


def pair(fn, a, b, flt, by="namespace, name", clamp=False):
    """sum by (...) of fn(a) + fn(b), each over [5m]. Two names in one __name__ regex would make rate()
    return identical labelsets (rate drops the name) and Prometheus rejects that with HTTP 422."""
    def one(m):
        e = "%s(%s%s[5m])" % (fn, m, flt)
        return "sum%s (%s)" % (" by (%s)" % by if by else "", "clamp_min(%s, 0)" % e if clamp else e)
    return "%s + %s" % (one(a), one(b))


def launcher_psi(resource, kind="waiting", sel=None):
    """% of time the VM's virt-launcher compute container stalled on `resource` (cgroup PSI, from cAdvisor)."""
    return launcher_name('100 * sum by (namespace, pod) (rate(container_pressure_%s_%s_seconds_total{%s}[5m]))'
                         % (resource, kind, sel or LP))


def steps(*pairs):
    return [{"color": c, "value": v} for c, v in pairs]


OK_WARN_BAD = lambda w, b: steps(("green", None), ("orange", w), ("red", b))  # noqa: E731


class Dash:
    def __init__(self, uid, title, description, tags, variables, links=None):
        self.d = {
            "uid": uid, "title": TITLE_PREFIX + title, "description": description, "tags": tags + [TAG],
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
        metrics = set()
        for t in targets:
            for pat in re.findall(r'__name__=~"([^"]+)"', t[0]):
                metrics.update(expand_alternation(pat))
            metrics.update(m for m in re.findall(r"\b((?:kubevirt|node|container|kube)_[A-Za-z0-9_]+)",
                                                 re.sub(r'__name__=~"[^"]+"', "", t[0])))
        metrics = sorted(metrics)
        self.manifest.append({"dashboard": self.d["uid"], "panel": title, "metrics": metrics,
                              "psi": "PSI" in title})

    def ts(self, title, desc, targets, unit="short", w=8, h=8, minv=0, maxv=None, thr=None,
           link=False, stack=False, fill=10, legend=True):
        if "PSI" in title and not WITH_PSI:
            return
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
        if "PSI" in title and not WITH_PSI:
            return
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



TABLE_EMPTY = {
    "Degraded or faulted Longhorn volumes": "All volumes are healthy",
    "Idle VM candidates": "No idle VMs at these thresholds",
    "Under-sized VM candidates": "No under-sized VMs at these thresholds",
    "PVCs used by neither a VM nor a pod (candidates)": "None found",
    "Large, mostly empty guest filesystems": "None found",
}


def _table(self, title, desc, cols, w=24, h=10, sort=None, labels=None, vm_link=False):
    """Table fed by instant queries that share the same label set. cols = [(expr, header, unit, thresholds)].
    The queries are joined with the `merge` transformation, so every expression must aggregate by the same
    labels. `labels` renames label columns, e.g. {"namespace": "Namespace", "name": "VM"}."""
    labels = labels or {"namespace": "Namespace", "name": "VM"}
    cols = [c for c in cols if WITH_PSI or "PSI" not in c[1]]
    self._record(title, desc, [(c[0], c[1]) for c in cols])
    single = len(cols) == 1
    rename = dict(labels)
    overrides = []
    order = {"Time": 0}
    for i, (expr, header, unit, thr) in enumerate(cols):
        rename["Value" if single else "Value #%s" % chr(65 + i)] = header
    for i, name in enumerate(list(labels.values()) + [c[1] for c in cols]):
        order[name] = i + 1
    for expr, header, unit, thr in cols:
        props = [{"id": "unit", "value": unit}, {"id": "decimals", "value": 1 if unit in ("percent", "short") else 0}]
        if thr:
            props += [{"id": "thresholds", "value": {"mode": "absolute", "steps": thr}},
                      {"id": "custom.cellOptions", "value": {"type": "color-background", "mode": "basic"}}]
        overrides.append({"matcher": {"id": "byName", "options": header}, "properties": props})
    if vm_link and "VM" in labels.values():
        overrides.append({"matcher": {"id": "byName", "options": "VM"}, "properties": [{"id": "links", "value": [{
            "title": "Open VM detail",
            "url": "/d/harvester-vm-detail-v2?var-namespace=${__data.fields.Namespace}&var-vm=${__value.text}"
                   "&${__url_time_range}"}]}]})
    self.d["panels"].append({
        "type": "table", "title": title, "description": desc, "datasource": DS,
        "id": self._next_id(), "gridPos": self._place(w, h),
        "targets": [{"refId": chr(65 + i), "datasource": DS, "expr": c[0], "format": "table",
                     "instant": True, "range": False} for i, c in enumerate(cols)],
        "transformations": [
            {"id": "merge", "options": {}},
            {"id": "organize", "options": {"excludeByName": {n: True for n in (
                "Time", "__name__", "container", "endpoint", "instance", "job", "pod", "service", "prometheus")},
                                           "renameByName": rename,
                                           "indexByName": order}}],
        "fieldConfig": {"defaults": {"custom": {"align": "auto", "filterable": True, "minWidth": 60,
                                                "cellOptions": {"type": "auto"}},
                                     **({"noValue": TABLE_EMPTY[title]} if title in TABLE_EMPTY else {})},
                        "overrides": overrides},
        "options": {"showHeader": True, "cellHeight": "sm", "footer": {"show": False},
                    "sortBy": [{"displayName": sort[0], "desc": sort[1]}] if sort else []},
    })


Dash.table = _table


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


def var_window(default="3d"):
    opts = ["1d", "3d", "5d"]
    return {"type": "custom", "name": "window", "label": "Look-back window", "query": ",".join(opts), "hide": 0,
            "current": {"selected": True, "text": default, "value": default},
            "options": [{"selected": o == default, "text": o, "value": o} for o in opts]}


def var_text(name, label, default):
    return {"type": "textbox", "name": name, "label": label, "query": default, "hide": 0,
            "current": {"selected": False, "text": default, "value": default},
            "options": [{"selected": True, "text": default, "value": default}]}


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
    d.stat("VMs: CPU Ready > 5%", "VMs whose vCPUs spend more than 5% of the time runnable but not "
           "scheduled. vSphere guidance: >5% per vCPU is a warning, >10% is a problem.",
           "count((%s) > 5) or vector(0)" % cpu_ready(), thr=OK_WARN_BAD(1, 5))
    d.stat("Worst CPU Ready", "Highest CPU Ready % of any VM in the selection.",
           "max(%s)" % cpu_ready(), unit="percent", thr=OK_WARN_BAD(5, 10))
    d.stat("VMs: guest mem > 90%", "VMs whose guest OS reports less than 10% memory available "
           "(MemAvailable, so page cache is NOT counted as used). Needs qemu-guest-agent.",
           "count((%s) > 90) or vector(0)" % guest_mem_pct(), thr=OK_WARN_BAD(1, 3))
    d.stat("Max VM memory PSI", "Highest share of the last 5 minutes in which a VM's launcher "
           "cgroup stalled waiting for memory (kernel PSI via cAdvisor). Any sustained value means the VM is "
           "being reclaimed or is thrashing on the host.",
           "max(%s)" % launcher_psi("memory"), unit="percent", thr=OK_WARN_BAD(1, 10))
    d.stat("Max VM CPU PSI", "Highest share of the last 5 minutes in which a VM's launcher cgroup "
           "waited for a CPU (PSI via cAdvisor): CPU limit throttling plus run-queue wait.",
           "max(%s)" % launcher_psi("cpu"), unit="percent", thr=OK_WARN_BAD(10, 25))
    d.stat("OOM kills (1h)", "Kernel OOM kills on the nodes (any process, including pods killed at their memory limit) plus OOM "
           "events of virt-launcher compute containers in the last hour. A launcher OOM kill takes the VM down.",
           "round(sum(increase(node_vmstat_oom_kill[1h]) %s) + "
           "(sum(increase(container_oom_events_total{%s}[1h])) or vector(0)))" % (NJ, LP),
           thr=steps(("green", None), ("red", 1)))

    d.stat("VMs near memory limit", "VMs whose virt-launcher working set is above 90% "
           "of its memory limit: OOM-kill risk for the VM.",
           "count((%s) > 90) or vector(0)" % launcher_ws_pct(), thr=OK_WARN_BAD(1, 3))
    d.stat("VMs throttled > 5%", "VMs whose launcher hit its CPU limit in more than 5% of scheduling periods.",
           "count((%s) > 5) or vector(0)" % launcher_name(
               '100 * sum by (namespace, pod) (rate(container_cpu_cfs_throttled_periods_total{%s}[5m])) '
               '/ sum by (namespace, pod) (rate(container_cpu_cfs_periods_total{%s}[5m]))' % (LP, LP)),
           thr=OK_WARN_BAD(1, 3))

    d.stat("Unhealthy volumes", "Longhorn volumes that are degraded or faulted (a replica is missing or "
           "rebuilding). Writes slow down while that lasts.", "count(%s) or vector(0)" % UNHEALTHY_VOLS,
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
    d.ts("VM CPU pressure (PSI, top N)", "% of time the VM's launcher cgroup waited for CPU "
         "(container_pressure_cpu_waiting_seconds_total). Complements CPU Ready with the host-cgroup view.",
         [("topk($topn, %s)" % launcher_psi("cpu"), "{{name}}")], unit="percent",
         thr=OK_WARN_BAD(10, 25), link=True)
    d.ts("Host CPU pressure (PSI) per node [needs kernel PSI]", "Kernel "
         "pressure-stall information: % of time tasks waited for CPU. Empty for nodes "
         "whose kernel boots without PSI (psi=1).",
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
         [("topk($topn, %s)" % pair("delta", "kubevirt_vmi_memory_swap_in_traffic_bytes", "kubevirt_vmi_memory_swap_out_traffic_bytes", F, clamp=True), "{{name}}")],
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
    d.ts("VM memory pressure (PSI, top N)", "% of time the VM's launcher cgroup stalled on memory: "
         "'some' (waiting) per VM. Host-side memory contention as seen by each VM (cAdvisor; needs "
         "PSI on the node the VM runs on).",
         [("topk($topn, %s)" % launcher_psi("memory"), "{{name}}")], unit="percent",
         thr=OK_WARN_BAD(1, 10), link=True)
    d.ts("Host memory available %", "MemAvailable / MemTotal per node.",
         [("(100 * node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) %s" % NJ, "{{nodename}}")],
         unit="percent", maxv=100, thr=steps(("red", None), ("orange", 10), ("green", 20)))
    d.ts("Host memory pressure (PSI) per node [needs kernel PSI]", "'some': at least one task waited for memory; 'full': all "
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
    d.ts("Longhorn read latency per VM (top N)", "Latency Longhorn itself measures per volume (the storage layer "
         "under the VM disk), worst volume of each VM, in ms. Compare with the VM-level latency above: if only "
         "this one is high, the problem is replicas/disks/network between nodes, not the guest.",
         [("topk($topn, (%s) / 1e6)" % lh_vm("longhorn_volume_read_latency"), "{{name}}")], unit="ms",
         thr=OK_WARN_BAD(20, 50), link=True)
    d.ts("Longhorn write latency per VM (top N)", "As above for writes. Writes wait for every replica, so a slow "
         "or rebuilding replica shows here first.",
         [("topk($topn, (%s) / 1e6)" % lh_vm("longhorn_volume_write_latency"), "{{name}}")], unit="ms",
         thr=OK_WARN_BAD(20, 50), link=True)
    d.ts("Longhorn IOPS per VM (top N)", "Read + write IOPS of the VM's Longhorn volumes.",
         [("topk($topn, %s)" % lh_vm("(longhorn_volume_read_iops + longhorn_volume_write_iops)", "sum"), "{{name}}")],
         unit="iops", link=True)
    d.ts("Node disk latency (worst device per node)", "Average time per I/O on the node's physical disks "
         "(await). Equivalent of vSphere's physical device latency.",
         [(disk_await(NJ), "{{nodename}}")], unit="ms", thr=OK_WARN_BAD(20, 50), w=12)
    d.ts("Node disk utilisation (busiest device per node)", "% of time the busiest physical disk of the node had "
         "I/O in flight. Sustained 90-100% means the disk is the bottleneck.",
         [(disk_util(NJ), "{{nodename}}")], unit="percent", maxv=100, thr=OK_WARN_BAD(70, 90), w=12)
    d.table("Degraded or faulted Longhorn volumes", "Volumes whose replicas are not all healthy. Detached "
            "volumes report 'unknown' and are not listed. Empty is good.",
            [("max by (pvc_namespace, pvc, state) (longhorn_volume_actual_size_bytes * on (volume) group_left "
              "(state) (%s))" % UNHEALTHY_VOLS, "Actual size", "bytes", None)],
            labels={"pvc_namespace": "Namespace", "pvc": "PVC", "state": "State"}, h=5)
    d.ts("VM I/O pressure (PSI, top N)", "% of time the VM's launcher cgroup waited for block I/O.",
         [("topk($topn, %s)" % launcher_psi("io"), "{{name}}")], unit="percent",
         thr=OK_WARN_BAD(10, 30), link=True)
    d.ts("Host I/O pressure (PSI) per node [needs kernel PSI]", "% of time tasks "
         "waited for block I/O on the node.",
         [("100 * rate(node_pressure_io_waiting_seconds_total[5m]) %s" % NJ, "{{nodename}} some"),
          ("100 * rate(node_pressure_io_stalled_seconds_total[5m]) %s" % NJ, "{{nodename}} full")],
         unit="percent", thr=OK_WARN_BAD(10, 30))
    d.ts("Network drops/s per VM (top N)", "vNIC rx + tx dropped packets.",
         [("topk($topn, %s)" % pair("rate", "kubevirt_vmi_network_receive_packets_dropped_total",
                                    "kubevirt_vmi_network_transmit_packets_dropped_total", F), "{{name}}")],
         unit="pps", link=True, w=12)
    d.ts("Network errors/s per VM (top N)", "vNIC rx + tx errors.",
         [("topk($topn, %s)" % pair("rate", "kubevirt_vmi_network_receive_errors_total",
                                    "kubevirt_vmi_network_transmit_errors_total", F), "{{name}}")],
         unit="pps", link=True, w=12)
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
         [(pair("delta", "kubevirt_vmi_memory_swap_in_traffic_bytes",
                "kubevirt_vmi_memory_swap_out_traffic_bytes", one, by="", clamp=True), "swap bytes / 5m"),
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
         [(pair("rate", "kubevirt_vmi_network_receive_packets_dropped_total",
                "kubevirt_vmi_network_transmit_packets_dropped_total", one, by="interface"), "{{interface}}")],
         unit="pps", w=8)
    d.ts("Errors/s", "rx + tx errors.",
         [(pair("rate", "kubevirt_vmi_network_receive_errors_total",
                "kubevirt_vmi_network_transmit_errors_total", one, by="interface"), "{{interface}}")],
         unit="pps", w=8)

    d.row("Host this VM runs on")
    ps1 = lp1
    d.ts("VM cgroup pressure (PSI)", "% of time this VM's launcher cgroup stalled waiting for CPU, memory "
         "or I/O ('some'), from cAdvisor.",
         [("100 * sum(rate(container_pressure_%s_waiting_seconds_total{%s}[5m]))" % (r, ps1), r)
          for r in ("cpu", "memory", "io")], unit="percent", thr=OK_WARN_BAD(10, 25), w=24)
    nj1 = '* on (instance) group_left (nodename) max by (instance, nodename) (node_uname_info{nodename="$node"})'
    d.ts("Node pressure (PSI) [needs kernel PSI]", "CPU, memory and I/O pressure of "
         "the hosting node ('some'). Empty if that node's kernel boots without PSI (psi=1).",
         [("100 * rate(node_pressure_cpu_waiting_seconds_total[5m]) %s" % nj1, "cpu"),
          ("100 * rate(node_pressure_memory_waiting_seconds_total[5m]) %s" % nj1, "memory"),
          ("100 * rate(node_pressure_io_waiting_seconds_total[5m]) %s" % nj1, "io")],
         unit="percent", thr=OK_WARN_BAD(10, 25), w=12)
    d.ts("Node memory available %", "MemAvailable / MemTotal of the hosting node.",
         [("(100 * node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) %s" % nj1, "available")],
         unit="percent", maxv=100, thr=steps(("red", None), ("orange", 10), ("green", 20)), w=12)
    d.ts("Node major faults/s and OOM kills", "node_vmstat_pgmajfault rate and OOM kills of the hosting node.",
         [("(rate(node_vmstat_pgmajfault[5m])) %s" % nj1, "major faults/s"),
          ("(increase(node_vmstat_oom_kill[5m])) %s" % nj1, "OOM kills / 5m")], unit="short", w=12)
    return d


# --------------------------------------------------------------------------- right-sizing
GIB = 1073741824
F2 = '{namespace=~"$namespace",name=~"$vm"}'
VMI = 'max by (namespace, name) (kubevirt_vmi_info%s)' % F2   # currently running VMIs
WITNESS = 'kube_node_labels{label_node_role_harvesterhci_io_witness="true"}'
STOPPED = 'kubevirt_vm_info{status_group="non_running",namespace=~"$namespace"}'
STOP_TS = 'kubevirt_vm_non_running_status_last_transition_timestamp_seconds{namespace=~"$namespace"}'


def running(e):
    return "(%s) and on (namespace, name) %s" % (e, VMI)


def avg_w(e):
    return "avg_over_time((%s)[$window:5m])" % e


def p95_w(e):
    return "quantile_over_time(0.95, (%s)[$window:5m])" % e


def max_w(e):
    return "max_over_time((%s)[$window:5m])" % e


VCPU = ("count by (namespace, name) (group by (namespace, name, id) "
        "(kubevirt_vmi_vcpu_seconds_total%s))" % F2)
CORES = "sum by (namespace, name) (rate(kubevirt_vmi_cpu_usage_seconds_total%s[5m]))" % F2
MEM_USED = ("max by (namespace, name) (kubevirt_vmi_memory_available_bytes%s) "
            "- max by (namespace, name) (kubevirt_vmi_memory_usable_bytes%s)" % (F2, F2))
MEM_ALLOC = "max by (namespace, name) (kubevirt_vmi_memory_domain_bytes%s)" % F2
NET_BPS = ("8 * (sum by (namespace, name) (rate(kubevirt_vmi_network_receive_bytes_total%s[5m])) "
           "+ sum by (namespace, name) (rate(kubevirt_vmi_network_transmit_bytes_total%s[5m])))" % (F2, F2))
IOPS = ("sum by (namespace, name) (rate(kubevirt_vmi_storage_iops_read_total%s[5m])) "
        "+ sum by (namespace, name) (rate(kubevirt_vmi_storage_iops_write_total%s[5m]))" % (F2, F2))


def pct_of_vcpus(core_expr):
    return "100 * (%s) / (%s)" % (core_expr, VCPU)


CPU_P95_PCT = running(pct_of_vcpus(p95_w(CORES)))
CPU_AVG_PCT = running(pct_of_vcpus(avg_w(CORES)))
CPU_MAX_PCT = running(pct_of_vcpus(max_w(CORES)))
SUGGEST_VCPU = running("clamp_min(ceil((%s) / ($target / 100)), 1)" % p95_w(CORES))
RECLAIM_VCPU = running("clamp_min((%s) - (%s), 0)" % (VCPU, SUGGEST_VCPU))
MEM_P95 = running(p95_w(MEM_USED))
MEM_P95_PCT = running("100 * (%s) / (%s)" % (p95_w(MEM_USED), MEM_ALLOC))
SUGGEST_MEM = running("clamp_min(ceil((%s) / ($target / 100) / %d), 1) * %d" % (p95_w(MEM_USED), GIB, GIB))
RECLAIM_MEM = running("clamp_min((%s) - (%s), 0)" % (MEM_ALLOC, SUGGEST_MEM))
IDLE = running("((%s) < $idle_cpu) and on (namespace, name) ((%s) / 1000 < $idle_net) "
               "and on (namespace, name) ((%s) < $idle_iops)"
               % (pct_of_vcpus(p95_w(CORES)), avg_w(NET_BPS), avg_w(IOPS)))
UNDER = running("((%s) > $under_cpu) or on (namespace, name) ((100 * (%s) / (%s)) > $under_mem)"
                % (pct_of_vcpus(p95_w(CORES)), p95_w(MEM_USED), MEM_ALLOC))


def of(e, set_expr):
    """Restrict e to the VMs present in set_expr."""
    return "(%s) and on (namespace, name) (%s)" % (e, set_expr)


def rightsizing():
    d = Dash(
        "harvester-rightsizing-v1", "Harvester Right-Sizing",
        "Idle, over- and under-provisioned VMs, with a suggested size and the vCPU/memory that could be reclaimed. "
        "Based on p95 usage over the look-back window; Prometheus in the Harvester add-on keeps only 5 days.",
        ["harvester", "kubevirt", "rightsizing"],
        [var_ds(),
         var_query("namespace", "Namespace", "label_values(kubevirt_vmi_info, namespace)", all_value=".*"),
         var_query("vm", "VM", 'label_values(kubevirt_vmi_info{namespace=~"$namespace"}, name)', all_value=".+"),
         var_window(),
         var_text("target", "Target utilisation % (sizing)", "70"),
         var_text("idle_cpu", "Idle: CPU p95 below %", "5"),
         var_text("idle_net", "Idle: network avg below kbit/s", "50"),
         var_text("idle_iops", "Idle: disk IOPS avg below", "5"),
         var_text("under_cpu", "Under-sized: CPU p95 above %", "85"),
         var_text("under_mem", "Under-sized: memory p95 above %", "90")],
        links=[{"title": "Capacity & Reclaim", "type": "link", "url": "/d/harvester-capacity-v1",
                "icon": "external link", "targetBlank": False},
               {"title": "VM Contention", "type": "link", "url": "/d/harvester-vm-contention-v1",
                "icon": "external link", "targetBlank": False}])

    d.row("Summary (look-back window and thresholds are the variables above)")
    d.stat("Running VMs", "VMs with a running instance in the selection.", "count(%s) or vector(0)" % VMI)
    d.stat("Idle VM candidates", "Running VMs whose CPU p95, average network and average disk IOPS are all "
           "below the idle thresholds for the whole window. Candidates to power off or remove, to confirm with "
           "the owner: a VM that works only occasionally (monthly jobs) looks idle in a short window.",
           "count(%s) or vector(0)" % IDLE, thr=OK_WARN_BAD(1, 3))
    d.stat("Oversized VMs", "Running VMs whose p95 usage would fit in fewer vCPUs or less "
           "memory at the target utilisation.",
           "count((%s) > 0 or on (namespace, name) (%s) > 0) or vector(0)" % (RECLAIM_VCPU, RECLAIM_MEM),
           thr=OK_WARN_BAD(1, 5))
    d.stat("vCPUs reclaimable", "Sum over VMs of (provisioned vCPUs - vCPUs needed for p95 CPU at the target "
           "utilisation).", "sum(%s) or vector(0)" % RECLAIM_VCPU)
    d.stat("Memory reclaimable", "Sum over VMs of (allocated memory - memory needed for p95 guest usage at the "
           "target utilisation). Only VMs running qemu-guest-agent report guest memory.",
           "sum(%s) or vector(0)" % RECLAIM_MEM, unit="bytes")
    d.stat("Under-sized VMs", "VMs above the under-sized CPU or memory p95 thresholds.",
           "count(%s) or vector(0)" % UNDER, thr=OK_WARN_BAD(1, 3))

    d.row("Provisioned vs actually used (fleet, selected VMs)")
    d.ts("vCPUs provisioned vs cores used", "Provisioned vCPUs of running VMs against the CPU cores they use.",
         [("sum(%s)" % running(VCPU), "vCPUs provisioned"),
          ("sum(%s)" % running(CORES), "cores used")], unit="short", w=12, fill=0)
    d.ts("Memory allocated vs guest memory used", "Memory allocated to running VMs against guest memory in use "
         "(available - MemAvailable, so reclaimable page cache is not counted as used; guest agent needed).",
         [("sum(%s)" % MEM_ALLOC.replace("max by (namespace, name)", "max by (namespace, name)"), "allocated"),
          ("sum(%s)" % running(MEM_USED), "used by guests")], unit="bytes", w=12, fill=0)

    d.row("Idle VM candidates")
    d.table("Idle VM candidates", "All three tests passed over the window: CPU p95 < idle threshold, average "
            "network < idle kbit/s, average disk IOPS < idle IOPS.",
            [(of(VCPU, IDLE), "vCPUs", "short", None),
             (of(MEM_ALLOC, IDLE), "Memory", "bytes", None),
             (of(pct_of_vcpus(p95_w(CORES)), IDLE), "CPU p95 %", "percent", None),
             (of(pct_of_vcpus(avg_w(CORES)), IDLE), "CPU avg %", "percent", None),
             (of("(%s) / 1000" % avg_w(NET_BPS), IDLE), "Net avg kbit/s", "short", None),
             (of(avg_w(IOPS), IDLE), "IOPS avg", "short", None)],
            sort=("vCPUs", True), vm_link=True, h=8)

    d.row("Right-sizing: provisioned vs p95 usage")
    d.table("Right-sizing recommendations (running VMs)", "Suggested = p95 usage / target utilisation, rounded up "
            "(vCPUs to whole cores, memory to whole GiB). A suggestion, not a promise: the window is short, and "
            "memory columns are empty for VMs without qemu-guest-agent. CPU peak % shows how far spikes went.",
            [(running(VCPU), "vCPUs", "short", None),
             (CPU_AVG_PCT, "CPU avg %", "percent", None),
             (CPU_P95_PCT, "CPU p95 %", "percent", None),
             (CPU_MAX_PCT, "CPU peak %", "percent", None),
             (SUGGEST_VCPU, "Sugg. vCPUs", "short", None),
             (RECLAIM_VCPU, "vCPU saving", "short", steps(("green", None), ("orange", 1), ("red", 4))),
             (running(MEM_ALLOC), "Mem", "bytes", None),
             (MEM_P95, "Mem p95", "bytes", None),
             (SUGGEST_MEM, "Sugg. mem", "bytes", None),
             (RECLAIM_MEM, "Mem saving", "bytes", None)],
            sort=("vCPU saving", True), vm_link=True, h=12)

    d.row("Under-provisioned VMs")
    d.table("Under-sized VM candidates", "If a VM was resized inside the window its p95 reflects the old size too. CPU p95 above the under-sized CPU threshold, or guest memory p95 above "
            "the memory threshold, over the window. Check CPU Ready on the contention dashboard before adding vCPUs.",
            [(of(running(VCPU), UNDER), "vCPUs", "short", None),
             (of(CPU_P95_PCT, UNDER), "CPU p95 %", "percent", steps(("green", None), ("orange", 70), ("red", 85))),
             (of(CPU_MAX_PCT, UNDER), "CPU peak %", "percent", None),
             (of(running(MEM_ALLOC), UNDER), "Memory", "bytes", None),
             (of(MEM_P95_PCT, UNDER), "Memory p95 %", "percent", steps(("green", None), ("orange", 80), ("red", 90)))],
            sort=("CPU p95 %", True), vm_link=True, h=7)
    return d


# --------------------------------------------------------------------------- capacity and reclaim
def hosts(e):
    """Restrict a per-node series to the nodes that can run VMs (everything but the witness)."""
    return "(%s) unless on (node) %s" % (e, WITNESS)


ALLOC_CPU = 'kube_node_status_allocatable{resource="cpu"}'
ALLOC_MEM = 'kube_node_status_allocatable{resource="memory"}'
REQ_MEM = 'sum by (node) (kube_pod_container_resource_requests{resource="memory"})'
VCPU_BY_NODE = ("sum by (node) (count by (namespace, name, node) (group by (namespace, name, node, id) "
                "(kubevirt_vmi_vcpu_seconds_total)))")
MEM_BY_NODE = "sum by (node) (max by (namespace, name, node) (kubevirt_vmi_memory_domain_bytes))"
HOST_JOIN = "* on (instance) group_left (nodename) max by (instance, nodename) (node_uname_info)"
HOST_CPU = "100 * (1 - avg by (instance) (rate(node_cpu_seconds_total{mode=\"idle\"}[5m]))) %s" % HOST_JOIN
HOST_MEM = ("100 * (1 - max by (instance) (node_memory_MemAvailable_bytes) "
            "/ max by (instance) (node_memory_MemTotal_bytes)) %s" % HOST_JOIN)
VOL_TO_VM = ('sum by (namespace, name) (label_replace(label_replace(%s, "namespace", "$1", "pvc_namespace", "(.*)"), '
             '"persistentvolumeclaim", "$1", "pvc", "(.*)") * on (namespace, persistentvolumeclaim) group_left (name) '
             '(max by (namespace, persistentvolumeclaim, name) (kubevirt_vm_disk_allocated_size_bytes) * 0 + 1))')
DAYS_STOPPED = ("max by (namespace, name) ((time() - %s) / 86400 and on (namespace, name) (%s > 0) "
                "and on (namespace, name) %s)" % (STOP_TS, STOP_TS, STOPPED))
STOPPED_DISK = "sum by (namespace, name) (kubevirt_vm_disk_allocated_size_bytes) and on (namespace, name) %s" % STOPPED
STOPPED_ACTUAL = "(%s) and on (namespace, name) %s" % (VOL_TO_VM % "longhorn_volume_actual_size_bytes", STOPPED)
ORPHAN_PVC = ('kube_persistentvolumeclaim_info{storageclass=~"harvester-.*",namespace=~"$namespace",'
              'namespace!~"cattle-.*|harvester-.*|kube-.*|longhorn-system"} '
              'unless on (namespace, persistentvolumeclaim) kubevirt_vm_disk_allocated_size_bytes '
              'unless on (namespace, persistentvolumeclaim) kube_pod_spec_volumes_persistentvolumeclaims_info')


DETACHED_TOP = ('topk(15, longhorn_volume_actual_size_bytes and on (volume) '
                '(longhorn_volume_state{state="detached"} == 1))')


def capacity():
    d = Dash(
        "harvester-capacity-v1", "Harvester Capacity & Reclaim",
        "Overcommit and N-1 headroom per host, host utilisation, stopped VMs and the storage they hold, "
        "Longhorn thin-provisioning efficiency and volumes/PVCs nobody uses. Harvester 1.8.x.",
        ["harvester", "kubevirt", "capacity"],
        [var_ds(),
         var_query("namespace", "Namespace", "label_values(kubevirt_vm_info, namespace)", all_value=".*"),
         var_window()],
        links=[{"title": "Right-Sizing", "type": "link", "url": "/d/harvester-rightsizing-v1",
                "icon": "external link", "targetBlank": False},
               {"title": "VM Contention", "type": "link", "url": "/d/harvester-vm-contention-v1",
                "icon": "external link", "targetBlank": False}])

    d.row("Cluster capacity (hosts = every node except the witness)")
    d.stat("vCPU : physical core", "vCPUs provisioned to running VMs divided by the allocatable CPU cores of the "
           "hosts. Harvester overcommits CPU by default (the add-on's overcommit setting), so 3:1 is normal.",
           "sum(count by (namespace, name) (group by (namespace, name, id) (kubevirt_vmi_vcpu_seconds_total))) "
           "/ sum(%s)" % hosts(ALLOC_CPU), thr=steps(("green", None), ("orange", 4), ("red", 8)))
    d.stat("Memory allocated", "Memory allocated to running VMs / allocatable memory of the hosts.",
           "100 * sum(max by (namespace, name) (kubevirt_vmi_memory_domain_bytes)) / sum(%s)" % hosts(ALLOC_MEM),
           unit="percent", thr=OK_WARN_BAD(70, 90))
    d.stat("Memory requested", "Sum of pod memory requests on the hosts / allocatable memory: what the "
           "scheduler sees.", "100 * sum(%s) / sum(%s)" % (hosts(REQ_MEM), hosts(ALLOC_MEM)), unit="percent",
           thr=OK_WARN_BAD(70, 90))
    d.stat("N-1 memory headroom", "Memory requests / (allocatable - the largest "
           "host). Above 100% the cluster cannot reschedule everything if that host fails.",
           "100 * sum(%s) / (sum(%s) - max(%s))" % (hosts(REQ_MEM), hosts(ALLOC_MEM), hosts(ALLOC_MEM)),
           unit="percent", thr=OK_WARN_BAD(80, 100))
    d.stat("Stopped VMs", "VMs that are not running.", "count(%s) or vector(0)" % STOPPED)
    d.stat("Longhorn scheduled", "Storage scheduled (replicas included) / usable disk capacity of "
           "the Longhorn nodes. Longhorn refuses new volumes beyond its over-provisioning limit.",
           "100 * sum(longhorn_node_storage_scheduled_bytes) / sum(longhorn_node_storage_capacity_bytes "
           "- longhorn_node_storage_reservation_bytes)", unit="percent", thr=OK_WARN_BAD(80, 100))

    d.row("Overcommit per host")
    d.ts("vCPUs provisioned vs allocatable cores", "Per host: vCPUs of the running VMs on it against the node's "
         "allocatable CPU.",
         [(VCPU_BY_NODE, "{{node}} provisioned vCPUs"), (hosts(ALLOC_CPU), "{{node}} allocatable cores")],
         unit="short", w=12, fill=0)
    d.ts("Memory allocated vs allocatable", "Per host: memory of the running VMs on it against the node's "
         "allocatable memory.",
         [(MEM_BY_NODE, "{{node}} allocated to VMs"), (hosts(ALLOC_MEM), "{{node}} allocatable")],
         unit="bytes", w=12, fill=0)

    d.row("Host utilisation")
    d.ts("Host CPU utilisation", "% of CPU time not idle, per node.", [(HOST_CPU, "{{nodename}}")],
         unit="percent", maxv=100, thr=OK_WARN_BAD(70, 90), w=12)
    d.ts("Host memory used", "100 - MemAvailable %, per node.", [(HOST_MEM, "{{nodename}}")],
         unit="percent", maxv=100, thr=OK_WARN_BAD(80, 92), w=12)
    d.table("Host utilisation over the look-back window", "Average and p95 of host CPU and memory use. A host "
            "that stays low at p95 has room to take VMs; one that is high at p95 has none.",
            [("max by (nodename) (avg_over_time((%s)[$window:5m]))" % HOST_CPU, "CPU avg %", "percent", None),
             ("max by (nodename) (quantile_over_time(0.95, (%s)[$window:5m]))" % HOST_CPU, "CPU p95 %", "percent",
              steps(("green", None), ("orange", 70), ("red", 90))),
             ("max by (nodename) (avg_over_time((%s)[$window:5m]))" % HOST_MEM, "Memory avg %", "percent", None),
             ("max by (nodename) (quantile_over_time(0.95, (%s)[$window:5m]))" % HOST_MEM, "Memory p95 %", "percent",
              steps(("green", None), ("orange", 80), ("red", 92)))],
            labels={"nodename": "Node"}, sort=("CPU p95 %", True), h=6)

    d.row("Stopped VMs and the storage they hold")
    d.stat("Stopped: provisioned", "Provisioned size of the disks of VMs that are not running.",
           "sum(%s) or vector(0)" % STOPPED_DISK, unit="bytes")
    d.stat("Stopped: actually used", "Longhorn actual (thin) size of those disks, one replica.",
           "sum(%s) or vector(0)" % STOPPED_ACTUAL, unit="bytes")
    d.stat("Stopped > 30 days", "Stopped VMs whose last transition is more than 30 days ago.",
           "count((%s) > 30) or vector(0)" % DAYS_STOPPED, thr=OK_WARN_BAD(1, 5))
    d.table("Stopped VMs", "Days since the VM stopped (blank when Harvester has no timestamp), disk size "
            "provisioned and actually used. Candidates to delete, archive or export.",
            [(DAYS_STOPPED, "Days stopped", "short", steps(("green", None), ("orange", 30), ("red", 90))),
             (STOPPED_DISK, "Disk provisioned", "bytes", None),
             (STOPPED_ACTUAL, "Disk used", "bytes", None)],
            sort=("Disk used", True), vm_link=True, w=14, h=9)

    d.row("Storage efficiency (Longhorn)")
    d.stat("Thin usage", "Longhorn volumes' actual size over their capacity.",
           "100 * sum(longhorn_volume_actual_size_bytes) / sum(longhorn_volume_capacity_bytes)", unit="percent")
    d.stat("Volumes detached", "Longhorn volumes not attached to a workload right now (includes the disks of "
           "stopped VMs).", 'count(longhorn_volume_state{state="detached"} == 1) or vector(0)')
    d.ts("Longhorn node storage", "Per node: disk usage, storage scheduled (replicas included) and capacity.",
         [("longhorn_node_storage_usage_bytes", "{{node}} used"),
          ("longhorn_node_storage_scheduled_bytes", "{{node}} scheduled"),
          ("longhorn_node_storage_capacity_bytes", "{{node}} capacity")], unit="bytes", w=14, fill=0)
    d.table("PVCs used by neither a VM nor a pod (candidates)", "PVCs on Harvester storage classes, outside "
            "system namespaces, that no VM disk and no pod references. Review before deleting: a PVC can be "
            "bound for a purpose this dashboard cannot see.",
            [("max by (namespace, persistentvolumeclaim) (kube_persistentvolumeclaim_resource_requests_storage_bytes "
              "and on (namespace, persistentvolumeclaim) (%s))" % ORPHAN_PVC, "Size", "bytes", None)],
            labels={"namespace": "Namespace", "persistentvolumeclaim": "PVC"}, sort=("Size", True), w=12, h=7)
    d.table("Largest detached Longhorn volumes", "Detached volumes by actual size (disks of stopped VMs "
            "included): where the space sits while nothing uses it.",
            [("max by (pvc_namespace, pvc) (%s)" % DETACHED_TOP, "Actual size", "bytes", None),
             ("max by (pvc_namespace, pvc) (longhorn_volume_capacity_bytes and on (volume) (%s))" % DETACHED_TOP,
              "Capacity", "bytes", None)],
            labels={"pvc_namespace": "Namespace", "pvc": "PVC"}, sort=("Actual size", True), w=12, h=7)
    fs = '{namespace=~"$namespace"}'
    fs_used = "max by (namespace, name, mount_point) (kubevirt_vmi_filesystem_used_bytes%s)" % fs
    fs_size = "max by (namespace, name, mount_point) (kubevirt_vmi_filesystem_capacity_bytes%s)" % fs
    fs_set = "((100 * %s / %s) < 20) and on (namespace, name, mount_point) (%s > 10737418240)" % (fs_used, fs_size, fs_size)
    d.table("Large, mostly empty guest filesystems", "Guest filesystems over 10 GiB that are under 20% used "
            "(guest agent): disks provisioned far larger than needed.",
            [("(100 * %s / %s) and on (namespace, name, mount_point) (%s)" % (fs_used, fs_size, fs_set),
              "Used %", "percent", None),
             ("(%s) and on (namespace, name, mount_point) (%s)" % (fs_size, fs_set), "Size", "bytes", None)],
            labels={"namespace": "Namespace", "name": "VM", "mount_point": "Mount"}, sort=("Used %", False),
            vm_link=True, h=6)
    return d


# --------------------------------------------------------------------------- Longhorn per VM + scorecard
def lh_vm(metric_expr, agg="max", flt=F2):
    """Map a per-volume Longhorn series to its VM through the PVC the VM disk uses (labels namespace, name)."""
    return ('%s by (namespace, name) (label_replace(label_replace(%s, "namespace", "$1", "pvc_namespace", "(.*)"), '
            '"persistentvolumeclaim", "$1", "pvc", "(.*)") * on (namespace, persistentvolumeclaim) group_left (name) '
            '(max by (namespace, persistentvolumeclaim, name) (kubevirt_vm_disk_allocated_size_bytes%s) * 0 + 1))'
            % (agg, metric_expr, flt))


DISKS = 'device=~"nvme[0-9]+n[0-9]+|sd[a-z]+"'
UNHEALTHY_VOLS = 'longhorn_volume_robustness{state=~"degraded|faulted"} == 1'


def disk_await(nj):
    """Average ms per I/O of the worst disk per node. Idle disks (no I/O) yield no sample instead of +Inf."""
    io = ("(rate(node_disk_reads_completed_total{%s}[5m]) + rate(node_disk_writes_completed_total{%s}[5m]))"
          % (DISKS, DISKS))
    return ("max by (nodename) (1000 * (rate(node_disk_read_time_seconds_total{%s}[5m]) + "
            "rate(node_disk_write_time_seconds_total{%s}[5m])) / (%s > 0) %s)" % (DISKS, DISKS, io, nj))


def disk_util(nj):
    return "max by (nodename) (100 * rate(node_disk_io_time_seconds_total{%s}[5m]) %s)" % (DISKS, nj)


def scorecard():
    ready = running("100 * sum by (namespace, name) (rate(kubevirt_vmi_vcpu_delay_seconds_total%s[5m])) / (%s)"
                    % (F2, VCPU))
    gmem = running("100 * (1 - max by (namespace, name) (kubevirt_vmi_memory_usable_bytes%s) "
                   "/ max by (namespace, name) (kubevirt_vmi_memory_available_bytes%s))" % (F2, F2))
    launcher = "max by (namespace, name) (%s)" % launcher_ws_pct()
    vm_wlat = running("1000 * sum by (namespace, name) (rate(kubevirt_vmi_storage_write_times_seconds_total%s[5m])) "
                      "/ (sum by (namespace, name) (rate(kubevirt_vmi_storage_iops_write_total%s[5m])) > 0)" % (F2, F2))
    lh_wlat = running("(%s) / 1e6" % lh_vm("longhorn_volume_write_latency"))
    drops = running(pair("rate", "kubevirt_vmi_network_receive_packets_dropped_total",
                         "kubevirt_vmi_network_transmit_packets_dropped_total", F2))
    unhealthy = lh_vm(UNHEALTHY_VOLS, "sum")
    nobackup = lh_vm("(longhorn_volume_last_backup_at == bool 0)", "sum")
    flag = lambda e, thr, name: 'label_replace((%s) > bool %s, "k", "%s", "", "")' % (e, thr, name)
    flags = "sum by (namespace, name) (%s)" % " or ".join([
        flag(ready, 5, "ready"), flag(gmem, 90, "mem"), flag(launcher, 90, "launcher"), flag(lh_wlat, 20, "lat"),
        flag(drops, 10, "drops"), flag(unhealthy, 0, "volumes")])
    d = Dash(
        "harvester-vm-scorecard-v1", "Harvester VM Scorecard",
        "One row per running VM: contention, storage health, backups and right-sizing in a single sortable table. "
        "'Flags' counts the warning thresholds a VM is over. Blank means no data or none (for example no "
        "qemu-guest-agent).",
        ["harvester", "kubevirt", "scorecard"],
        [var_ds(),
         var_query("namespace", "Namespace", "label_values(kubevirt_vmi_info, namespace)", all_value=".*"),
         var_query("vm", "VM", 'label_values(kubevirt_vmi_info{namespace=~"$namespace"}, name)', all_value=".+"),
         var_window(), var_text("target", "Target utilisation % (sizing)", "70")],
        links=[{"title": "VM Contention", "type": "link", "url": "/d/harvester-vm-contention-v1",
                "icon": "external link", "targetBlank": False},
               {"title": "Right-Sizing", "type": "link", "url": "/d/harvester-rightsizing-v1",
                "icon": "external link", "targetBlank": False}])
    d.row("Fleet")
    d.stat("Running VMs", "VMs with a running instance in the selection.", "count(%s) or vector(0)" % VMI)
    d.stat("VMs flagged", "Running VMs over at least one warning threshold.",
           "count((%s) > 0) or vector(0)" % flags, thr=OK_WARN_BAD(1, 3))
    d.stat("Unhealthy volumes", "Longhorn volumes that are degraded or faulted (detached volumes report 'unknown' "
           "and are not counted).", "count(%s) or vector(0)" % UNHEALTHY_VOLS, thr=steps(("green", None), ("red", 1)))
    d.stat("Never backed up", "Volumes of VM disks with no Longhorn backup ever recorded.",
           "sum(%s) or vector(0)" % nobackup, thr=OK_WARN_BAD(1, 10))
    d.row("VM scorecard (click a column header to sort; the VM name opens its detail dashboard)")
    d.table("VM scorecard",
            "Flags = number of these over their warning level: CPU Ready > 5%, guest memory > 90%, launcher memory "
            "> 90% of limit, Longhorn write latency > 20 ms, network drops > 10/s, unhealthy volumes. The saving "
            "columns come from the right-sizing dashboard (look-back window and target utilisation variables).",
            [(flags, "Flags", "short", steps(("green", None), ("orange", 1), ("red", 3))),
             (running(VCPU), "vCPUs", "short", None),
             (running(MEM_ALLOC), "Mem", "bytes", None),
             (running(pct_of_vcpus(CORES)), "CPU used %", "percent", steps(("green", None), ("orange", 70), ("red", 90))),
             (ready, "CPU Ready %", "percent", steps(("green", None), ("orange", 5), ("red", 10))),
             (gmem, "Guest mem %", "percent", steps(("green", None), ("orange", 80), ("red", 95))),
             (launcher, "Launcher mem %", "percent", steps(("green", None), ("orange", 90), ("red", 98))),
             (vm_wlat, "Disk write ms", "short", steps(("green", None), ("orange", 20), ("red", 50))),
             (lh_wlat, "Longhorn write ms", "short", steps(("green", None), ("orange", 20), ("red", 50))),
             (running(IOPS), "IOPS", "short", None),
             (drops, "Net drops/s", "short", steps(("green", None), ("orange", 1), ("red", 10))),
             (running(unhealthy), "Unhealthy vols", "short", steps(("green", None), ("red", 1))),
             (running(nobackup), "No-backup vols", "short", steps(("green", None), ("orange", 1))),
             (RECLAIM_VCPU, "vCPU saving", "short", None),
             (RECLAIM_MEM, "Mem saving", "bytes", None),
             (launcher_psi_by_vm("cpu"), "CPU PSI %", "percent", steps(("green", None), ("orange", 10), ("red", 25))),
             (launcher_psi_by_vm("memory"), "Mem PSI %", "percent", steps(("green", None), ("orange", 1), ("red", 10))),
             (launcher_psi_by_vm("io"), "I/O PSI %", "percent", steps(("green", None), ("orange", 10), ("red", 30)))],
            sort=("Flags", True), vm_link=True, h=18)
    return d


def launcher_psi_by_vm(resource):
    return "max by (namespace, name) (%s)" % launcher_psi(resource)


# --------------------------------------------------------------------------- alert rules (PrometheusRule)
# Thresholds are written as __TOKENS__ and substituted by the chart from values.yaml (alerts.thresholds).
ALERT_TOKENS = {  # token -> (values key, default)
    "CPU_READY_WARN": ("cpuReadyWarn", 5), "CPU_READY_CRIT": ("cpuReadyCritical", 10),
    "GUEST_MEM": ("guestMemoryPercent", 95), "LAUNCHER_MEM": ("launcherMemoryPercent", 95),
    "VM_DISK_LATENCY_MS": ("vmDiskLatencyMs", 50), "LH_LATENCY_MS": ("longhornLatencyMs", 50),
    "NET_DROPS": ("vmNetworkDropsPerSecond", 10), "HOST_CPU_PSI": ("hostCpuPressurePercent", 30),
    "HOST_MEM_PSI": ("hostMemoryPressurePercent", 10), "HOST_IO_PSI": ("hostIoPressurePercent", 40),
    "HOST_REQ_MEM": ("hostMemoryRequestsPercent", 90), "LH_SCHED_WARN": ("longhornScheduledWarn", 90),
    "LH_SCHED_CRIT": ("longhornScheduledCritical", 100), "LH_NODE_USED": ("longhornNodeUsedPercent", 80),
    "DISK_AWAIT_MS": ("diskAwaitMs", 50), "DISK_UTIL": ("diskUtilPercent", 95),
}
NODE_NAME = "* on (instance) group_left (nodename) max by (instance, nodename) (node_uname_info)"


def alert_rules():
    vcpu = VCPU.replace(F2, "")
    ready = "100 * sum by (namespace, name) (rate(kubevirt_vmi_vcpu_delay_seconds_total[5m])) / (%s)" % vcpu
    gmem = ("100 * (1 - max by (namespace, name) (kubevirt_vmi_memory_usable_bytes) "
            "/ max by (namespace, name) (kubevirt_vmi_memory_available_bytes))")
    lp = 'container="compute",pod=~"virt-launcher-.*"'
    launcher = ('max by (namespace, name) (label_replace(100 * container_memory_working_set_bytes{%s} '
                '/ on (namespace, pod, container) kube_pod_container_resource_limits{resource="memory",%s}, '
                '"name", "$1", "pod", "virt-launcher-(.*)-[a-z0-9]{5}"))' % (lp, lp))
    vm_wlat = ("1000 * sum by (namespace, name) (rate(kubevirt_vmi_storage_write_times_seconds_total[5m])) / "
               "(sum by (namespace, name) (rate(kubevirt_vmi_storage_iops_write_total[5m])) > 0)")
    lh_lat = "(%s) / 1e6" % lh_vm("longhorn_volume_write_latency", flt="")
    drops = pair("rate", "kubevirt_vmi_network_receive_packets_dropped_total",
                 "kubevirt_vmi_network_transmit_packets_dropped_total", "")
    psi = lambda r: "100 * rate(node_pressure_%s_waiting_seconds_total[5m]) %s" % (r, NODE_NAME)
    n1 = "100 * sum(%s) / (sum(%s) - max(%s))" % (hosts(REQ_MEM), hosts(ALLOC_MEM), hosts(ALLOC_MEM))
    lh_usable = "(longhorn_node_storage_capacity_bytes - longhorn_node_storage_reservation_bytes)"

    def rule(name, expr, for_, sev, summary, desc):
        return {"alert": name, "expr": expr, "for": for_, "labels": {"severity": sev},
                "annotations": {"summary": summary, "description": desc}}

    vm = "VM {{ $labels.namespace }}/{{ $labels.name }}"
    node = "node {{ $labels.nodename }}"
    return {"groups": [
        {"name": "harvester-extra.vm-contention", "rules": [
            rule("HarvesterVMCPUReadyHigh", "(%s) > __CPU_READY_WARN__" % ready, "15m", "warning",
                 vm + " is waiting for a CPU",
                 vm + " has spent {{ $value | printf \"%.1f\" }}% of the time runnable but not scheduled for 15 minutes "
                      "(warning above __CPU_READY_WARN__%). The host CPUs are oversubscribed; see the VM Contention dashboard."),
            rule("HarvesterVMCPUReadyCritical", "(%s) > __CPU_READY_CRIT__" % ready, "10m", "critical",
                 vm + " is starved of CPU",
                 vm + " CPU Ready is {{ $value | printf \"%.1f\" }}% (critical above __CPU_READY_CRIT__%)."),
            rule("HarvesterVMGuestMemoryHigh", "(%s) > __GUEST_MEM__" % gmem, "15m", "warning",
                 vm + " is running out of memory",
                 vm + " uses {{ $value | printf \"%.1f\" }}% of its memory (MemAvailable, page cache not counted; needs "
                      "qemu-guest-agent). Above __GUEST_MEM__% for 15 minutes."),
            rule("HarvesterVMLauncherMemoryNearLimit", "(%s) > __LAUNCHER_MEM__" % launcher, "10m", "critical",
                 vm + " is close to being OOM-killed",
                 "The virt-launcher of " + vm + " uses {{ $value | printf \"%.1f\" }}% of its memory limit. If it "
                 "reaches the limit the VM is killed."),
            rule("HarvesterVMLauncherOOMKilled",
                 'increase(container_oom_events_total{%s}[10m]) > 0' % lp, "0m", "critical",
                 "virt-launcher {{ $labels.pod }} was OOM-killed",
                 "The VM behind pod {{ $labels.namespace }}/{{ $labels.pod }} hit its memory limit and was killed."),
            rule("HarvesterVMDiskLatencyHigh", "(%s) > __VM_DISK_LATENCY_MS__" % vm_wlat, "10m", "warning",
                 vm + " disk writes are slow",
                 vm + " average write latency is {{ $value | printf \"%.0f\" }} ms (above __VM_DISK_LATENCY_MS__ ms)."),
            rule("HarvesterVMLonghornLatencyHigh", "(%s) > __LH_LATENCY_MS__" % lh_lat, "10m", "warning",
                 vm + " storage layer is slow",
                 "Longhorn reports {{ $value | printf \"%.0f\" }} ms write latency on a volume of " + vm +
                 " (above __LH_LATENCY_MS__ ms): check replicas, rebuilds and node disks."),
            rule("HarvesterVMNetworkDrops", "(%s) > __NET_DROPS__" % drops, "10m", "warning",
                 vm + " is dropping packets",
                 vm + " drops {{ $value | printf \"%.1f\" }} packets/s on its vNICs."),
        ]},
        {"name": "harvester-extra.host-contention", "rules": [
            rule("HarvesterNodeOOMKills",
                 "(increase(node_vmstat_oom_kill[15m]) %s) > 0" % NODE_NAME, "0m", "warning",
                 node + " killed processes for lack of memory",
                 "{{ $value | printf \"%.0f\" }} OOM kills on " + node + " in the last 15 minutes (any pod or process)."),
            rule("HarvesterHostMemoryPressure", "(%s) > __HOST_MEM_PSI__" % psi("memory"), "10m", "warning",
                 node + " is under memory pressure",
                 "Tasks on " + node + " waited for memory {{ $value | printf \"%.1f\" }}% of the time (PSI; needs "
                 "psi=1 on the kernel command line)."),
            rule("HarvesterHostCPUPressure", "(%s) > __HOST_CPU_PSI__" % psi("cpu"), "15m", "warning",
                 node + " CPUs are saturated",
                 "Runnable tasks on " + node + " waited for a CPU {{ $value | printf \"%.1f\" }}% of the time (PSI)."),
            rule("HarvesterHostIOPressure", "(%s) > __HOST_IO_PSI__" % psi("io"), "15m", "warning",
                 node + " is waiting on disk I/O",
                 "Tasks on " + node + " waited for block I/O {{ $value | printf \"%.1f\" }}% of the time (PSI)."),
            rule("HarvesterNodeDiskLatencyHigh", "(%s) > __DISK_AWAIT_MS__" % disk_await(NODE_NAME), "15m", "warning",
                 node + " disks are slow",
                 "Worst physical disk of " + node + " averages {{ $value | printf \"%.0f\" }} ms per I/O."),
            rule("HarvesterNodeDiskBusy", "(%s) > __DISK_UTIL__" % disk_util(NODE_NAME), "30m", "warning",
                 node + " has a saturated disk",
                 "A physical disk of " + node + " has been busy {{ $value | printf \"%.0f\" }}% of the time."),
        ]},
        {"name": "harvester-extra.capacity-and-storage", "rules": [
            rule("HarvesterMemoryN1Exceeded", "(%s) > 100" % n1, "1h", "warning",
                 "The cluster cannot lose its biggest host",
                 "Memory requests are {{ $value | printf \"%.0f\" }}% of what the cluster would have after losing "
                 "its largest host: that host's workloads could not be rescheduled."),
            rule("HarvesterHostMemoryRequestsHigh",
                 "(100 * (%s) / on (node) (%s)) > __HOST_REQ_MEM__" % (hosts(REQ_MEM), hosts(ALLOC_MEM)), "30m", "warning",
                 "Node {{ $labels.node }} memory is almost fully requested",
                 "Pod memory requests on {{ $labels.node }} are {{ $value | printf \"%.0f\" }}% of its allocatable memory."),
            rule("HarvesterLonghornSchedulingHigh",
                 "(100 * sum(longhorn_node_storage_scheduled_bytes) / sum(%s)) > __LH_SCHED_WARN__" % lh_usable,
                 "30m", "warning", "Longhorn storage is almost fully scheduled",
                 "{{ $value | printf \"%.0f\" }}% of the usable disk space is already promised to volume replicas."),
            rule("HarvesterLonghornSchedulingFull",
                 "(100 * sum(longhorn_node_storage_scheduled_bytes) / sum(%s)) > __LH_SCHED_CRIT__" % lh_usable,
                 "15m", "critical", "Longhorn cannot schedule more replicas",
                 "{{ $value | printf \"%.0f\" }}% of the usable disk space is scheduled: new volumes and rebuilds can fail."),
            rule("HarvesterLonghornNodeStorageHigh",
                 "(100 * longhorn_node_storage_usage_bytes / %s) > __LH_NODE_USED__" % lh_usable, "30m", "warning",
                 "Longhorn disks of node {{ $labels.node }} are filling up",
                 "Node {{ $labels.node }} uses {{ $value | printf \"%.0f\" }}% of its Longhorn disk space."),
            rule("HarvesterLonghornVolumeDegraded", 'longhorn_volume_robustness{state="degraded"} == 1', "30m", "warning",
                 "Longhorn volume {{ $labels.pvc_namespace }}/{{ $labels.pvc }} is degraded",
                 "A replica of volume {{ $labels.volume }} (PVC {{ $labels.pvc_namespace }}/{{ $labels.pvc }}) is "
                 "missing or still rebuilding for 30 minutes."),
            rule("HarvesterLonghornVolumeFaulted", 'longhorn_volume_robustness{state="faulted"} == 1', "5m", "critical",
                 "Longhorn volume {{ $labels.pvc_namespace }}/{{ $labels.pvc }} is faulted",
                 "Volume {{ $labels.volume }} (PVC {{ $labels.pvc_namespace }}/{{ $labels.pvc }}) has no healthy replica."),
        ]},
    ]}


def main():
    global WITH_PSI
    manifest = []
    for psi in (False, True):
        WITH_PSI = psi
        os.makedirs(OUT[psi], exist_ok=True)
        panels = 0
        for name, dash in (("harvester-vm-contention", contention()), ("harvester-vm-detail-v2", detail()),
                           ("harvester-rightsizing", rightsizing()), ("harvester-capacity", capacity()),
                           ("harvester-vm-scorecard", scorecard())):
            with open(os.path.join(OUT[psi], name + ".json"), "w") as f:
                json.dump(dash.d, f, indent=2)
                f.write("\n")
            panels += len(dash.manifest)
            if psi:  # the PSI variant is a superset, so it describes every panel
                manifest += dash.manifest
        print("%-15s %d panels" % (os.path.basename(OUT[psi]), panels))
    os.makedirs(os.path.join(CHART, "alerts"), exist_ok=True)
    with open(os.path.join(CHART, "alerts", "harvester-extra-alerts.json"), "w") as f:
        json.dump(alert_rules(), f, indent=2)
        f.write("\n")
    with open(os.path.join(HERE, "required-metrics.json"), "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")


if __name__ == "__main__":
    main()
