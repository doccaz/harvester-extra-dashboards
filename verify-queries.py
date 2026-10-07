#!/usr/bin/env python3
"""Run every dashboard and alert query against a live Prometheus the way Grafana / Prometheus run them.

    kubectl -n cattle-monitoring-system port-forward svc/rancher-monitoring-prometheus 9090 &
    python3 verify-queries.py http://localhost:9090 [dashboards|dashboards-psi]

Graphs are run as a 6 h range query (step 60 s), tiles and tables as instant queries, alert rules as instant queries,
with the dashboard variables set to "All" and the documented defaults. This catches errors that only appear for a
real range, such as "found duplicate series for the match group" when kube-state-metrics or the kubelet restarted
within the window, which `validate.py` (syntax only) and instant queries both miss. Standard library only.
Exit code 1 if any query fails. An empty result is not an error (an idle cluster has no OOM kills).
"""
import glob
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
base = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:9090").rstrip("/")
variant = sys.argv[2] if len(sys.argv) > 2 else "dashboards-psi"
VARS = {"namespace": ".*", "vm": ".+", "node": ".*", "topn": "10", "window": "3d", "target": "70", "idle_cpu": "5",
        "idle_net": "50", "idle_iops": "5", "under_cpu": "85", "under_mem": "90", "rpo_days": "7", "wi_vcpu": "4",
        "wi_mem": "8", "wi_disk": "100", "wi_replicas": "2", "wi_fill": "50", "wi_overcommit": "1.75",
        "wi_overhead": "0.5", "wi_overprov": "200", "wi_minfree": "20", "wi_add": "0"}
ALERT_DEFAULTS = "50"   # any number: the thresholds only change which series pass, not whether the expression is valid


def run(expr, ranged):
    now = time.time()
    if ranged:
        params = {"query": expr, "start": now - 6 * 3600, "end": now, "step": 60}
        url = base + "/api/v1/query_range?"
    else:
        params = {"query": expr, "time": now}
        url = base + "/api/v1/query?"
    try:
        urllib.request.urlopen(url + urllib.parse.urlencode(params), timeout=120).read()
        return None
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())["error"]
        except Exception:  # noqa: BLE001
            return "HTTP %d" % e.code
    except Exception as e:  # noqa: BLE001
        return str(e)


total = bad = 0
for path in sorted(glob.glob(os.path.join(HERE, "charts", "*", variant, "*.json"))):
    d = json.load(open(path))
    for p in d["panels"]:
        for t in p.get("targets", []):
            expr = re.sub(r"\$\{?(\w+)\}?", lambda m: VARS.get(m.group(1), m.group(0)), t["expr"])
            ranged = not (t.get("instant") or t.get("format") == "table")
            total += 1
            err = run(expr, ranged)
            if err:
                bad += 1
                print("ERROR %-30s %-40s %s" % (d["uid"][:30], p["title"][:40], err[:160]))
for path in glob.glob(os.path.join(HERE, "charts", "*", "alerts", "*.json")):
    for g in json.load(open(path))["groups"]:
        for r in g["rules"]:
            total += 1
            err = run(re.sub(r"__[A-Z_]+__", ALERT_DEFAULTS, r["expr"]), False)
            if err:
                bad += 1
                print("ERROR alert %-34s %s" % (r["alert"], err[:160]))
print("%d queries run, %d errors" % (total, bad))
sys.exit(1 if bad else 0)
