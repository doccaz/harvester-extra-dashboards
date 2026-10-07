#!/usr/bin/env python3
"""Offline checks for the generated dashboards (no Prometheus or Grafana needed).

Needs `pip install promql-parser`. For both chart variants (dashboards, dashboards-psi) verifies JSON
structure, unique panel ids/uids, that every variable used in a query is defined, that every PromQL
expression parses, and that the PSI variant only adds panels to the default one.
"""
import glob
import json
import os
import re
import sys

import promql_parser

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE = {"namespace": "default", "vm": "vm1", "node": "node1", "topn": "10", "window": "3d", "target": "70",
          "idle_cpu": "5", "idle_net": "50", "idle_iops": "5", "under_cpu": "85", "under_mem": "90",
          "rpo_days": "7", "wi_vcpu": "4", "wi_mem": "8", "wi_disk": "100", "wi_replicas": "2", "wi_fill": "50",
          "wi_overcommit": "1.75", "wi_overhead": "0.5", "wi_overprov": "200", "wi_minfree": "20"}

errors = []
exprs = 0
titles = {}  # variant -> {uid: set(panel titles)}

variants = sorted(glob.glob(os.path.join(HERE, "charts", "*", "dashboards*")))
if [os.path.basename(v) for v in variants] != ["dashboards", "dashboards-psi"]:
    errors.append("expected the dashboards and dashboards-psi directories, found %s" % variants)

for vdir in variants:
    variant = os.path.basename(vdir)
    uids = set()  # unique within a variant; both variants intentionally carry the same uids
    titles[variant] = {}
    for path in sorted(glob.glob(os.path.join(vdir, "*.json"))):
        name = os.path.join(variant, os.path.basename(path))
        with open(path) as f:
            d = json.load(f)
        if d["uid"] in uids:
            errors.append("%s: duplicate uid %s" % (name, d["uid"]))
        uids.add(d["uid"])
        if d["uid"].startswith(("harvester-vm-dashboard", "harvester-vm-detail-dashboard")):
            errors.append("%s: uid collides with an official Harvester dashboard" % name)
        defined = {v["name"] for v in d["templating"]["list"]}
        ids = [p["id"] for p in d["panels"]]
        if len(ids) != len(set(ids)):
            errors.append("%s: duplicate panel ids" % name)
        titles[variant][d["uid"]] = {p["title"] for p in d["panels"]}
        for p in d["panels"]:
            for t in p.get("targets", []):
                expr = t["expr"]
                exprs += 1
                for var in re.findall(r"\$\{?([a-zA-Z_]+)", expr):
                    if var not in defined:
                        errors.append("%s / %s: undefined variable $%s" % (name, p["title"], var))
                q = re.sub(r"\$\{?(\w+)\}?", lambda m: SAMPLE.get(m.group(1), "x"), expr)
                try:
                    promql_parser.parse(q)
                except Exception as ex:  # noqa: BLE001
                    errors.append("%s / %s [%s]: %s\n    %s" % (name, p["title"], t["refId"], ex, q))
        for var in d["templating"]["list"]:
            if var["type"] == "query":
                for used in re.findall(r"\$([a-z]+)", var["definition"]):
                    if used not in defined:
                        errors.append("%s: variable %s uses undefined $%s" % (name, var["name"], used))

if len(titles) == 2:
    base, psi = titles["dashboards"], titles["dashboards-psi"]
    for uid, tset in base.items():
        if not tset <= psi.get(uid, set()):
            errors.append("%s: dashboards-psi lacks panels of the default variant: %s"
                          % (uid, sorted(tset - psi.get(uid, set()))))
        extra = psi.get(uid, set()) - tset
        if any("PSI" not in t for t in extra):
            errors.append("%s: the PSI variant adds non-PSI panels: %s" % (uid, sorted(extra)))
        if any("PSI" in t for t in tset):
            errors.append("%s: the default variant contains PSI panels" % uid)

# alert rules: every expression parses with its thresholds filled in, tokens are all handled by the template
rules_path = os.path.join(HERE, "charts", "harvester-extra-dashboards", "alerts", "harvester-extra-alerts.json")
template = open(os.path.join(HERE, "charts", "harvester-extra-dashboards", "templates", "prometheusrule.yaml")).read()
names = set()
with open(rules_path) as f:
    for grp in json.load(f)["groups"]:
        for r in grp["rules"]:
            for key in ("alert", "expr", "for", "labels", "annotations"):
                if key not in r:
                    errors.append("alert %s: missing %s" % (r.get("alert", "?"), key))
            if r["alert"] in names:
                errors.append("duplicate alert name %s" % r["alert"])
            names.add(r["alert"])
            if r["labels"].get("severity") not in ("warning", "critical"):
                errors.append("alert %s: severity must be warning or critical" % r["alert"])
            for token in re.findall(r"__([A-Z_]+)__", json.dumps(r)):
                if '"%s"' % token not in template:
                    errors.append("alert %s: token %s is not substituted by the chart template" % (r["alert"], token))
            try:
                promql_parser.parse(re.sub(r"__[A-Z_]+__", "1", r["expr"]))
                exprs += 1
            except Exception as ex:  # noqa: BLE001
                errors.append("alert %s: %s" % (r["alert"], ex))

print("%d variants, %d expressions checked (dashboards + %d alert rules)" % (len(variants), exprs, len(names)))
if errors:
    print("\n".join(errors))
    sys.exit(1)
print("OK")
