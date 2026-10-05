#!/usr/bin/env python3
"""Check against a live Prometheus which metrics the dashboards need and which exist.

    kubectl -n cattle-monitoring-system port-forward svc/rancher-monitoring-prometheus 9090 &
    python3 verify-metrics.py http://localhost:9090

Prints, per panel, the metrics that have no series, then the label/join assumptions the PromQL
relies on. Exit code 1 if any panel is missing a metric. Standard library only.
"""
import json
import os
import sys
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
base = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:9090").rstrip("/")


def query(q):
    url = "%s/api/v1/query?%s" % (base, urllib.parse.urlencode({"query": q}))
    with urllib.request.urlopen(url, timeout=30) as r:
        data = json.load(r)
    if data.get("status") != "success":
        raise RuntimeError(data)
    return data["data"]["result"]


with open(os.path.join(HERE, "required-metrics.json")) as f:
    manifest = json.load(f)

names = sorted({m for p in manifest for m in p["metrics"]})
present = {m: bool(query("count({__name__=\"%s\"})" % m)) for m in names}

missing_total = 0
for p in manifest:
    miss = [m for m in p["metrics"] if not present[m]]
    if miss:
        missing_total += 1
        print("MISSING  %-28s %-40s %s" % (p["dashboard"][:28], p["panel"][:40], ", ".join(miss)))
print("\n%d metrics checked, %d absent, %d panels affected\n"
      % (len(names), sum(not v for v in present.values()), missing_total))

checks = [
    ("VMI series carry the node label", 'count(kubevirt_vmi_info{node!=""})'),
    ("vCPU series have one series per id", "count(kubevirt_vmi_vcpu_seconds_total)"),
    ("vCPU delay exposed (needs schedstats)", "count(kubevirt_vmi_vcpu_delay_seconds_total)"),
    ("guest agent memory (usable) reported", "count(kubevirt_vmi_memory_usable_bytes)"),
    ("launcher compute containers in cAdvisor", 'count(container_memory_working_set_bytes{container="compute",pod=~"virt-launcher-.*"})'),
    ("launcher memory limits in kube-state-metrics", 'count(kube_pod_container_resource_limits{resource="memory",container="compute",pod=~"virt-launcher-.*"})'),
    ("node_uname_info joins node-exporter on instance", "count(node_pressure_cpu_waiting_seconds_total * on (instance) group_left (nodename) node_uname_info)"),
    ("CFS periods exposed for launcher", 'count(container_cpu_cfs_periods_total{container="compute",pod=~"virt-launcher-.*"})'),
    ("launcher OOM events exposed", 'count(container_oom_events_total{container="compute",pod=~"virt-launcher-.*"})'),
]
for label, q in checks:
    try:
        r = query(q)
        n = r[0]["value"][1] if r else "0"
    except Exception as e:  # noqa: BLE001
        n = "error: %s" % e
    print("%-52s %s" % (label, n))

# Unit sanity: a busy VM's usage % should not be ~0.1% (would indicate the /1000 seen in the stock panel).
print("\nPeak CPU usage % across VMs (sanity check of units):")
for r in query('max(100 * sum by (namespace,name) (rate(kubevirt_vmi_cpu_usage_seconds_total[5m]))'
               ' / count by (namespace,name) (group by (namespace,name,id) (kubevirt_vmi_vcpu_seconds_total)))'):
    print("  %.1f%%" % float(r["value"][1]))
sys.exit(1 if missing_total else 0)
