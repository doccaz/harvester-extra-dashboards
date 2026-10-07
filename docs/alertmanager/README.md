# Emailing the Harvester alerts

The `alerts.enabled` chart option creates the rules, but a default Harvester `rancher-monitoring` Alertmanager has
**one receiver, `"null"`, that discards everything**. This folder holds a tested config that emails only the
`Harvester*` alerts of this chart, and a throwaway in-cluster mail catcher to prove it works before any real SMTP
relay is involved. Nothing here is applied by the chart.

| File | What |
|---|---|
| `alertmanager.yaml` | The complete Alertmanager config: stock config + receiver `harvester-extra-email` + a route for `alertname =~ "Harvester.*"` |
| `mailpit.yaml` | [Mailpit](https://github.com/axllent/mailpit) `v1.31.4` Deployment + Service in `cattle-monitoring-system`: accepts mail on :1025 only with the login from Secret `mailpit-smtp`, shows it on :8025. Nothing leaves the cluster |

Checked offline: `amtool check-config` (Alertmanager 0.28.1, the version in Harvester v1.8.2) passes, and
`amtool config routes test` sends `Harvester*` alerts (warning and critical, with and without a `namespace` label) to
`harvester-extra-email` and `KubeCPUOvercommit`, `CPUThrottlingHigh`, `LonghornVolumeActualSpaceUsedWarning` and
`Watchdog` to `"null"`.

**Applied and tested on the lab (2026-10-07):** the catcher requires a random 40-character password; a login with no
credentials is rejected (530), and a wrong password or user is rejected (535). A hand-sent `HarvesterTestAlert` was
routed, authenticated and delivered: subject `[FIRING:1] HarvesterTestAlert labs (warning)`, Alertmanager's email
counter 1 sent / 0 failed.

## Apply it (catcher with a random password, no real email)

```bash
K="kubectl --context local -n cattle-monitoring-system"

# 1. random password, stored only in Secret mailpit-smtp (format user:password)
PW=$(openssl rand -base64 36 | tr -d '/+=\n' | cut -c1-40)
printf 'alertmanager:%s' "$PW" | $K create secret generic mailpit-smtp --from-file=smtp-auth=/dev/stdin

# 2. the catcher (it refuses logins without that credential)
kubectl --context local apply -f docs/alertmanager/mailpit.yaml

# 3. the Alertmanager config with the password filled in; back up the Secret, replace ONLY the alertmanager.yaml
#    key (the Secret also holds rancher_defaults.tmpl). Validate first: amtool check-config <file>
$K get secret alertmanager-rancher-monitoring-alertmanager -o yaml > alertmanager-secret.backup.yaml
sed "s|CHANGE_ME|$PW|" docs/alertmanager/alertmanager.yaml > /tmp/am.yaml
python3 -c "import base64,json;print(json.dumps({'data':{'alertmanager.yaml':base64.b64encode(open('/tmp/am.yaml','rb').read()).decode()}}))" > /tmp/patch.json
$K patch secret alertmanager-rancher-monitoring-alertmanager --type merge --patch-file /tmp/patch.json
rm -f /tmp/am.yaml /tmp/patch.json; unset PW

# 4. after about a minute: the receiver is loaded (the password shows as <secret>) and the reload succeeded
$K port-forward svc/rancher-monitoring-alertmanager 9093:9093 &
curl -s localhost:9093/api/v2/status | jq -r '.config.original' | grep -A8 harvester-extra-email
curl -s localhost:9093/metrics | grep alertmanager_config_last_reload_successful

# 5. send a test alert; after the 30 s group_wait it is mailed to the catcher
curl -s -XPOST localhost:9093/api/v2/alerts -H 'Content-Type: application/json' -d \
 '[{"labels":{"alertname":"HarvesterTestAlert","severity":"warning","namespace":"labs"},"annotations":{"summary":"routing test"}}]'
$K port-forward svc/mailpit 8025:8025 &      # open http://localhost:8025 (the web UI itself has no login)
```

Read the password back when you need it: `kubectl --context local -n cattle-monitoring-system get secret mailpit-smtp
-o jsonpath='{.data.smtp-auth}' | base64 -d`. It also sits in the Alertmanager config Secret (`auth_password`).
Keep it out of git: `alertmanager.yaml` in this folder only has the `CHANGE_ME` placeholder.

Undo: re-apply the backed-up Secret (`kubectl apply -f alertmanager-secret.backup.yaml`, after removing
`resourceVersion`/`uid`/`creationTimestamp` if apply complains), then
`kubectl --context local delete -f docs/alertmanager/mailpit.yaml` and `kubectl --context local -n cattle-monitoring-system delete secret mailpit-smtp`.

## Notes and caveats

- **Why the global config and not `AlertmanagerConfig` resources (or their UI):** the Prometheus operator adds
  `namespace="<the AlertmanagerConfig's namespace>"` to such routes by default, and these alerts carry the *VM's*
  namespace (`labs`) or none at all (N-1, node alerts), so they would not match.
- **Persistence (not verified):** the Secret is created by the monitoring chart, so redeploying the add-on may reset it.
  The durable place is the add-on's `valuesContent` (`alertmanager.config`). Test that before relying on it.
- **Other alerts:** the Kubernetes built-ins firing in the lab (`KubeCPUOvercommit`, `KubeMemoryOvercommit`,
  `CPUThrottlingHigh`, `KubeJobFailed`, `LonghornVolumeActualSpaceUsedWarning`) stay on `"null"`. Widen the matcher
  if you want them mailed.
- **Real email:** change `to`, `from`, `smarthost` and drop `require_tls: false`. Keep the password out of the config:
  create a Secret `alertmanager-smtp` with a `password` key, mount it through the Alertmanager `secrets` setting and use
  `auth_password_file`. Gmail needs an app password (2-step verification), and the cluster must be able to reach the
  relay on its SMTP port.
- Warning and critical rules have different names, so Alertmanager's "critical silences warning for the same
  `alertname`" inhibit rule does not hide a warning behind its critical.
