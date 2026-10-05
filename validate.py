#!/usr/bin/env python3
"""Offline checks for the generated dashboards (no Prometheus or Grafana needed).

Needs `pip install promql-parser`. Verifies JSON structure, unique panel ids/uids, that every
variable used in a query is defined, and that every PromQL expression parses.
"""
import glob
import json
import os
import re
import sys

import promql_parser

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE = {"namespace": "default", "vm": "vm1", "node": "node1", "topn": "10"}
BUILTIN = {"datasource", "__field", "__url_time_range", "__all", "__rate_interval", "__interval"}

errors = []
uids = set()
exprs = 0
for path in sorted(glob.glob(os.path.join(HERE, "dashboards", "*.json"))):
    name = os.path.basename(path)
    with open(path) as f:
        d = json.load(f)
    if d["uid"] in uids:
        errors.append("%s: duplicate uid %s" % (name, d["uid"]))
    uids.add(d["uid"])
    if d["uid"].startswith("harvester-vm-dashboard") or d["uid"].startswith("harvester-vm-detail-dashboard"):
        errors.append("%s: uid collides with an official Harvester dashboard" % name)
    defined = {v["name"] for v in d["templating"]["list"]}
    ids = [p["id"] for p in d["panels"]]
    if len(ids) != len(set(ids)):
        errors.append("%s: duplicate panel ids" % name)
    for p in d["panels"]:
        for t in p.get("targets", []):
            e = t["expr"]
            exprs += 1
            for v in re.findall(r"\$\{?([a-zA-Z_]+)", e):
                if v not in defined:
                    errors.append("%s / %s: undefined variable $%s" % (name, p["title"], v))
            q = re.sub(r"\$\{?(\w+)\}?", lambda m: SAMPLE.get(m.group(1), "x"), e)
            try:
                promql_parser.parse(q)
            except Exception as ex:  # noqa: BLE001
                errors.append("%s / %s [%s]: %s\n    %s" % (name, p["title"], t["refId"], ex, q))
    # query variables must parse as label_values(...) with defined variables
    for v in d["templating"]["list"]:
        if v["type"] == "query":
            for u in re.findall(r"\$([a-z]+)", v["definition"]):
                if u not in defined:
                    errors.append("%s: variable %s uses undefined $%s" % (name, v["name"], u))

print("%d dashboards, %d expressions checked" % (len(uids), exprs))
if errors:
    print("\n".join(errors))
    sys.exit(1)
print("OK")
