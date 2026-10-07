# Emailing the Harvester alerts (Alertmanager side)

The `alerts.enabled` chart option creates the alert rules, but a default Harvester `rancher-monitoring` Alertmanager has
**one receiver, `"null"`, that discards everything**, so nobody is told. This folder holds a tested Alertmanager config
that emails only the `Harvester*` alerts of this chart. The mail destination used in the lab is an in-cluster mail sink,
set up in [`../mailpit/`](../mailpit/README.md) (read that first: it creates the SMTP login this config uses).
Nothing here is applied by the chart.

| File | What |
|---|---|
| `alertmanager.yaml` | The complete Alertmanager config: the stock config + receiver `harvester-extra-email` + a route for `alertname =~ "Harvester.*"`. Holds a `CHANGE_ME` placeholder for the SMTP password, never the real one |

Checked offline with `amtool` 0.28.1 (the version in Harvester v1.8.2): `check-config` passes, and `config routes test`
sends `Harvester*` alerts (warning and critical, with and without a `namespace` label) to `harvester-extra-email` and
`KubeCPUOvercommit`, `CPUThrottlingHigh`, `LonghornVolumeActualSpaceUsedWarning` and `Watchdog` to `"null"`.
**Applied and tested on the lab (2026-10-07):** a hand-sent alert was routed, authenticated and delivered
(`[FIRING:1] HarvesterTestAlert labs (warning)`, email counter 1 sent / 0 failed), and later a real
`HarvesterVMLauncherMemoryNearLimit` critical alert arrived the same way.

## Apply it

Do the Mailpit install first (Secrets `mailpit-smtp` / `mailpit-ui`, the Deployment and the Ingress). Then:

```bash
K="kubectl --context local -n cattle-monitoring-system"

# 1. the config with the SMTP password filled in from the Mailpit Secret (never typed or committed)
PW=$($K get secret mailpit-smtp -o jsonpath='{.data.smtp-auth}' | base64 -d | cut -d: -f2-)
sed "s|CHANGE_ME|$PW|" docs/alertmanager/alertmanager.yaml > /tmp/am.yaml; unset PW

# 2. validate BEFORE touching the cluster; stop here if it does not say SUCCESS
chmod 644 /tmp/am.yaml
docker run --rm -v /tmp:/c:ro --entrypoint amtool prom/alertmanager:v0.28.1 check-config /c/am.yaml

# 3. back up the Secret, then replace ONLY its alertmanager.yaml key (it also holds rancher_defaults.tmpl)
$K get secret alertmanager-rancher-monitoring-alertmanager -o yaml > alertmanager-secret.backup.yaml
python3 -c "import base64,json;print(json.dumps({'data':{'alertmanager.yaml':base64.b64encode(open('/tmp/am.yaml','rb').read()).decode()}}))" > /tmp/patch.json
$K patch secret alertmanager-rancher-monitoring-alertmanager --type merge --patch-file /tmp/patch.json
rm -f /tmp/am.yaml /tmp/patch.json

# 4. after about a minute the receiver is loaded (the password shows as <secret>) and the reload succeeded
$K port-forward svc/rancher-monitoring-alertmanager 9093:9093 &
curl -s localhost:9093/api/v2/status | jq -r '.config.original' | grep -A8 harvester-extra-email
curl -s localhost:9093/metrics | grep alertmanager_config_last_reload_successful     # expect 1
```

### Send a test alert

```bash
curl -s -XPOST localhost:9093/api/v2/alerts -H 'Content-Type: application/json' -d \
 '[{"labels":{"alertname":"HarvesterTestAlert","severity":"warning","namespace":"labs"},"annotations":{"summary":"routing test"}}]'
```

After Alertmanager's 30 s group wait the mail appears in the Mailpit UI. The test alert resolves by itself after five
minutes and a *resolved* mail follows. Alertmanager's own delivery counters are
`alertmanager_notifications_total{integration="email"}` and `alertmanager_notifications_failed_total{integration="email",...}`.

### Undo

Re-apply the backed-up Secret (`kubectl apply -f alertmanager-secret.backup.yaml`, after removing
`resourceVersion`, `uid` and `creationTimestamp` if apply complains). To remove the mail sink as well, see "Operations"
in the Mailpit README.

## Notes and caveats

- **Why the global config and not `AlertmanagerConfig` resources (or their UI):** the Prometheus operator adds
  `namespace="<the AlertmanagerConfig's namespace>"` to such routes by default, and these alerts carry the *VM's*
  namespace (`labs`) or none at all (N-1, node alerts), so they would not match.
- **Persistence (not verified):** the Secret is created by the monitoring chart, so redeploying the add-on may reset
  it. The durable place is the add-on's `valuesContent` (`alertmanager.config`). Test that before relying on it.
- **Other alerts:** the Kubernetes built-ins firing in the lab (`KubeCPUOvercommit`, `KubeMemoryOvercommit`,
  `CPUThrottlingHigh`, `KubeJobFailed`, `LonghornVolumeActualSpaceUsedWarning`) stay on `"null"`. Widen the matcher if
  you want them mailed.
- **The SMTP password must match** Secret `mailpit-smtp`; rotating it means changing both (see the Mailpit README).
- **A real mailbox instead of the sink:** change `to`, `from` and `smarthost`, drop `require_tls: false`, and keep the
  password out of the config: create a Secret with a `password` key, mount it through the Alertmanager `secrets`
  setting and use `auth_password_file`. Gmail needs an app password (2-step verification), and the cluster must be able to
  reach the relay on its SMTP port. The commented lines in `alertmanager.yaml` show the shape.
- Warning and critical rules have different names, so Alertmanager's "critical silences warning for the same
  `alertname`" inhibit rule does not hide a warning behind its critical.
