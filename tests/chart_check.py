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
    assert sorted(d["metadata"]["name"] for d in docs) == sorted(names), (label, docs)
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


BOTH = ["rel-vm-contention", "rel-vm-detail-v2"]
check("default (no PSI)", [], "dashboards", BOTH)
check("psi.enabled", ["--set", "psi.enabled=true"], "dashboards-psi", BOTH)
check("only contention", ["--set", "dashboards.detail.enabled=false"], "dashboards", ["rel-vm-contention"])
docs = check("custom namespace/label/annotation",
             ["--set", "dashboardsNamespace=mon", "--set", "sidecar.label=my_label", "--set", "labels.team=virt",
              "--set-string", "annotations.k8s-sidecar-target-directory=/tmp/dashboards/Harvester"],
             "dashboards", BOTH, ns="mon", label_key="my_label")
meta = docs[0]["metadata"]
assert meta["labels"]["team"] == "virt" and "grafana_dashboard" not in meta["labels"]
assert meta["annotations"]["k8s-sidecar-target-directory"] == "/tmp/dashboards/Harvester"

out = subprocess.run(["helm", "template", "rel", CHART, "--set", "dashboards.contention.enabled=false",
                      "--set", "dashboards.detail.enabled=false"], capture_output=True, text=True)
assert out.returncode == 0 and "kind: ConfigMap" not in out.stdout
print("ok  %-30s no ConfigMaps rendered" % "all dashboards disabled")

with open(os.path.join(CHART, "dashboards", "harvester-vm-contention.json")) as f:
    assert '"legendFormat": "{{name}}"' in f.read()
print("ok  legends use {{name}} (kept verbatim)")
