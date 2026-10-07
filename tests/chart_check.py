#!/usr/bin/env python3
"""Render the Helm chart in several configurations and assert what comes out.

Needs `helm` and PyYAML. The key assertion: the JSON inside each ConfigMap is byte-identical to the file in
the chart (nothing evaluated the {{name}} legends), for both the default and the PSI variant.
"""
import json
import os
import subprocess
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHART = os.path.join(ROOT, "charts", "harvester-extra-dashboards")


def render(*args):
    out = subprocess.run(["helm", "template", "rel", CHART, *args], capture_output=True, text=True)
    if out.returncode:
        sys.exit("helm template failed: " + out.stderr)
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def check(label, args, variant, names, ns="cattle-dashboards", label_key="grafana_dashboard"):
    docs = render(*args)
    got = sorted(d["metadata"]["name"] for d in docs)
    assert got == sorted(names), (label, "got", got, "expected", sorted(names))
    for d in docs:
        assert d["kind"] == "ConfigMap" and d["metadata"]["namespace"] == ns, (label, d["metadata"])
        assert d["metadata"]["labels"][label_key] == "1", (label, d["metadata"]["labels"])
        (fname, content), = d["data"].items()
        with open(os.path.join(CHART, variant, fname)) as f:
            src = f.read()
        assert content == src.rstrip("\n"), "%s: ConfigMap content differs from %s/%s" % (label, variant, fname)
        json.loads(content)
    print("ok  %-30s %s" % (label, sorted(names)))
    return docs


BOTH = ["rel-vm-contention", "rel-vm-detail-v2", "rel-rightsizing", "rel-capacity", "rel-scorecard"]
check("default (no PSI)", [], "dashboards", BOTH)
check("psi.enabled", ["--set", "psi.enabled=true"], "dashboards-psi", BOTH)
check("only contention", ["--set", "dashboards.detail.enabled=false", "--set", "dashboards.rightsizing.enabled=false",
      "--set", "dashboards.capacity.enabled=false", "--set", "dashboards.scorecard.enabled=false"],
      "dashboards", ["rel-vm-contention"])
check("without contention and detail", ["--set", "dashboards.contention.enabled=false", "--set", "dashboards.detail.enabled=false"],
      "dashboards", ["rel-rightsizing", "rel-capacity", "rel-scorecard"])
docs = check("custom namespace/label/annotation",
             ["--set", "dashboardsNamespace=mon", "--set", "sidecar.label=my_label", "--set", "labels.team=virt",
              "--set-string", "annotations.k8s-sidecar-target-directory=/tmp/dashboards/Harvester"],
             "dashboards", BOTH, ns="mon", label_key="my_label")
meta = docs[0]["metadata"]
assert meta["labels"]["team"] == "virt" and "grafana_dashboard" not in meta["labels"]
assert meta["annotations"]["k8s-sidecar-target-directory"] == "/tmp/dashboards/Harvester"

out = subprocess.run(["helm", "template", "rel", CHART] + sum(
    [["--set", "dashboards.%s.enabled=false" % k] for k in ("contention", "detail", "rightsizing", "capacity", "scorecard")], []),
    capture_output=True, text=True)
assert out.returncode == 0 and "kind: ConfigMap" not in out.stdout
print("ok  %-30s no ConfigMaps rendered" % "all dashboards disabled")

# --- alerts: off by default, and rendered with substituted thresholds and untouched Prometheus templates
assert not any(d["kind"] == "PrometheusRule" for d in render()), "alerts must be opt-in"
docs = render("--set", "alerts.enabled=true", "--set", "alerts.thresholds.cpuReadyWarn=7", "--set", "alerts.namespace=mon")
rule_doc = [d for d in docs if d["kind"] == "PrometheusRule"]
assert len(rule_doc) == 1
pr = rule_doc[0]
assert pr["metadata"]["namespace"] == "mon" and pr["metadata"]["labels"]["release"] == "rancher-monitoring"
rules = [r for g in pr["spec"]["groups"] for r in g["rules"]]
with open(os.path.join(CHART, "alerts", "harvester-extra-alerts.json")) as f:
    source = [r for g in json.load(f)["groups"] for r in g["rules"]]
assert len(rules) == len(source) >= 20, (len(rules), len(source))
text = json.dumps(pr["spec"])
assert "__" not in text, "unsubstituted threshold token left: " + text[text.index("__") - 20: text.index("__") + 30]
ready = [r for r in rules if r["alert"] == "HarvesterVMCPUReadyHigh"][0]
assert ready["expr"].endswith("> 7"), ready["expr"]
assert "{{ $labels.name }}" in ready["annotations"]["description"], "Prometheus template was altered"
assert all(r["labels"]["severity"] in ("warning", "critical") and r["annotations"]["summary"] for r in rules)
print("ok  %-30s %d rules, thresholds substituted, {{ $labels }} kept, opt-in by default" % ("alerts", len(rules)))

with open(os.path.join(CHART, "dashboards", "harvester-vm-contention.json")) as f:
    assert '"legendFormat": "{{name}}"' in f.read()
print("ok  legends use {{name}} (kept verbatim)")
