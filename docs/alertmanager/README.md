# Emailing the Harvester alerts

The `alerts.enabled` chart option creates the rules, but a default Harvester `rancher-monitoring` Alertmanager has
**one receiver, `"null"`, that discards everything**. This folder holds a tested config that emails only the
`Harvester*` alerts of this chart, and a throwaway in-cluster mail catcher to prove it works before any real SMTP
relay is involved. Nothing here is applied by the chart.

| File | What |
|---|---|
| `alertmanager.yaml` | The complete Alertmanager config: stock config + receiver `harvester-extra-email` + a route for `alertname =~ "Harvester.*"` |
| `mailpit.yaml` | [Mailpit](https://github.com/axllent/mailpit) `v1.31.4` Deployment + Service in `cattle-monitoring-system`: accepts any mail on :1025, shows it on :8025. Nothing leaves the cluster |

Checked offline: `amtool check-config` (Alertmanager 0.28.1, the version in Harvester v1.8.2) passes, and
`amtool config routes test` sends `Harvester*` alerts (warning and critical, with and without a `namespace` label) to
`harvester-extra-email` and `KubeCPUOvercommit`, `CPUThrottlingHigh`, `LonghornVolumeActualSpaceUsedWarning` and
`Watchdog` to `"null"`. **Not yet applied to a cluster**, so the end-to-end mail delivery is untested.

## Try it (catcher, no real email)

```bash
K="kubectl --context local -n cattle-monitoring-system"

# 1. the catcher
kubectl --context local apply -f docs/alertmanager/mailpit.yaml

# 2. back up, then replace ONLY the alertmanager.yaml key (the Secret also holds rancher_defaults.tmpl)
$K get secret alertmanager-rancher-monitoring-alertmanager -o yaml > alertmanager-secret.backup.yaml
$K patch secret alertmanager-rancher-monitoring-alertmanager --type merge \
  -p "{\"data\":{\"alertmanager.yaml\":\"$(base64 -w0 docs/alertmanager/alertmanager.yaml)\"}}"

# 3. wait about a minute for the config reloader, then check the receiver is there
$K port-forward svc/rancher-monitoring-alertmanager 9093:9093 &
curl -s localhost:9093/api/v2/status | jq -r '.config.original' | grep -A3 harvester-extra-email

# 4. send a test alert; after the 30 s group_wait it is mailed to the catcher
curl -s -XPOST localhost:9093/api/v2/alerts -H 'Content-Type: application/json' -d \
 '[{"labels":{"alertname":"HarvesterTestAlert","severity":"warning","namespace":"labs"},"annotations":{"summary":"routing test"}}]'
$K port-forward svc/mailpit 8025:8025 &      # open http://localhost:8025
```

Undo: `kubectl --context local apply -f alertmanager-secret.backup.yaml` (strip `resourceVersion`/`uid` first if
apply complains) and `kubectl --context local delete -f docs/alertmanager/mailpit.yaml`.

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
