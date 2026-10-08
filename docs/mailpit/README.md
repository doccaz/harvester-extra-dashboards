# Mailpit for the Harvester lab

> **Not an official SUSE product; for exploration and evaluation only, provided as is.** See the
> [disclaimer](../../README.md).

[Mailpit](https://github.com/axllent/mailpit) is a mail sink with a web UI. In the lab it receives the alert emails that
Alertmanager sends, so you can read them in a browser **without any real SMTP relay**: nothing is delivered outside the
cluster. It is how the `Harvester*` alerts of this repo are made visible (see
[`../alertmanager/`](../alertmanager/README.md) for the Alertmanager side).

Everything below was applied and tested on the lab (Harvester v1.8.2, kube context `local`) on 2026-10-07.

## What runs where

| Thing | Name | Detail |
|---|---|---|
| Namespace | `cattle-monitoring-system` | next to Prometheus, Alertmanager and Grafana |
| Deployment | `mailpit` | `docker.io/axllent/mailpit:v1.31.4`, 1 replica, strategy `Recreate`; non-root (uid/gid/fsGroup 65534), read-only root filesystem, all capabilities dropped, seccomp `RuntimeDefault`; 20m/32Mi requests, 200m/128Mi limits |
| Volume | PVC `mailpit-data` | 1 GiB, storage class `harvester-2replicas` (the lab default), mounted at `/data` (SQLite database); keeps up to 500 messages (`MP_MAX_MESSAGES`) |
| Service `mailpit` | ClusterIP `:1025` | **SMTP**, what Alertmanager talks to (`mailpit.cattle-monitoring-system.svc:1025`); needs a login |
| Service `mailpit-web` | ClusterIP `:8025` | web UI and API, only reached through the Ingress or a port-forward |
| Ingress `mailpit` | `mailpit.conteudoquestionavel.org` | class `nginx`, path `/`, backend `mailpit-web:8025`, `tls` with the host and no secret (the controller's default certificate, like the `auth` and `kasten` Ingresses); read/send timeouts 3600 s for the live-update websocket |
| Secret `mailpit-smtp` | key `smtp-auth` | `alertmanager:<password>`, the SMTP login; read by Mailpit (`MP_SMTP_AUTH`) |
| Secret `mailpit-ui` | key `ui-auth` | `mailpit:<password>`, the web UI/API login (HTTP basic); read by Mailpit (`MP_UI_AUTH`) |

Both passwords are random 40-character strings that exist **only** in these Secrets (and the SMTP one also inside
Alertmanager's config Secret). They are never in git.

## How it is reached in the lab

```
browser --HTTPS--> Cloudflare --tunnel (cloudflared, ns cloudflare)--> https://192.168.86.250 (Harvester VIP, nginx ingress)
                                                                        --> Service mailpit-web:8025 --> Mailpit (login required)
Alertmanager --SMTP :1025 + login--> Service mailpit --> Mailpit --> PVC mailpit-data
```

- The Harvester VIP `192.168.86.250` serves the `nginx` Ingress class. It presents a self-signed certificate for every
  hostname, so browsers only see a valid one because **Cloudflare** terminates TLS in front.
- The lab's public hostnames go through a **remotely managed `cloudflared` tunnel** (deployment `cloudflared` in the
  `cloudflare` namespace, `TUNNEL_TOKEN`). Its routes live in Cloudflare Zero Trust, not in the cluster. For this host
  there is a *Public hostname* `mailpit.conteudoquestionavel.org` -> service `https://192.168.86.250` with **No TLS
  Verify** on, like the other lab hostnames. Cloudflare creates the DNS record.
- Cluster-internal traffic (Alertmanager -> Mailpit SMTP) is plain SMTP with a login, on the pod network.

## Install from scratch

```bash
K="kubectl --context local -n cattle-monitoring-system"

# 1. the two logins, random, stored only as Secrets
printf 'alertmanager:%s' "$(openssl rand -base64 36 | tr -d '/+=\n' | cut -c1-40)" | $K create secret generic mailpit-smtp --from-file=smtp-auth=/dev/stdin
printf 'mailpit:%s'      "$(openssl rand -base64 36 | tr -d '/+=\n' | cut -c1-40)" | $K create secret generic mailpit-ui   --from-file=ui-auth=/dev/stdin

# 2. edit the hostname (twice, at the bottom of the file) if it is not mailpit.conteudoquestionavel.org, then apply
kubectl --context local apply -f docs/mailpit/mailpit.yaml
$K rollout status deploy/mailpit

# 3. Cloudflare: add the Public hostname to the tunnel (see above). Until it exists, test from the LAN:
AUTH=$($K get secret mailpit-ui -o jsonpath='{.data.ui-auth}' | base64 -d)
printf 'user = "%s"\n' "$AUTH" | curl -sk -K - --resolve mailpit.conteudoquestionavel.org:443:192.168.86.250 \
  https://mailpit.conteudoquestionavel.org/api/v1/info | jq .
unset AUTH
```

Then point Alertmanager at it: [`../alertmanager/README.md`](../alertmanager/README.md).

## Using it

- **Web UI:** `https://mailpit.conteudoquestionavel.org`, user `mailpit`. Read the password with
  `kubectl --context local -n cattle-monitoring-system get secret mailpit-ui -o jsonpath='{.data.ui-auth}' | base64 -d`
  (the part after `mailpit:`).
- **API** (same login): `GET /api/v1/messages` lists mails, `GET /api/v1/message/<ID>` reads one, `DELETE /api/v1/messages`
  empties the mailbox.
- **Without the Ingress:** `kubectl --context local -n cattle-monitoring-system port-forward svc/mailpit-web 8025:8025`
  and open `http://localhost:8025` (the same login applies).
- **Send a test alert** and watch it arrive (after Alertmanager's 30 s group wait): see "Send a test alert" in the
  Alertmanager README.

## Operations

| Task | How |
|---|---|
| Rotate the **web UI** password | `$K delete secret mailpit-ui`, recreate it as in the install step, `$K rollout restart deploy/mailpit` |
| Rotate the **SMTP** password | change **both** Secret `mailpit-smtp` and `auth_password` in Alertmanager's config (the two must match), restart Mailpit; until both are updated Alertmanager's mails are refused and retried |
| Empty the mailbox | `DELETE /api/v1/messages` with the UI login (or delete the PVC) |
| Upgrade Mailpit | change the image tag in `mailpit.yaml`, `kubectl apply`; the volume is kept |
| Check mail delivery from Alertmanager | `alertmanager_notifications_total{integration="email"}` and `..._failed_total` on Alertmanager's `/metrics` |
| Remove it all | `kubectl --context local delete -f docs/mailpit/mailpit.yaml`, `$K delete secret mailpit-smtp mailpit-ui`, remove the Cloudflare route, and point Alertmanager's receiver elsewhere |

## Security notes

- The page shows the **full text of every alert mail**. Once the Cloudflare route exists it is on the public internet
  behind HTTP basic auth only. The long random password makes guessing impractical; a **Cloudflare Access** policy on
  that hostname is advisable on top. Mailpit has no rate limiting of its own.
- The SMTP login protects the sink from other pods in the cluster; SMTP itself is unencrypted on the pod network.
- The Mailpit database holds alert text, which can include VM and namespace names.

## What was tried and did not work (so you do not repeat it)

- **Through the Harvester / Kubernetes API proxy** (`/api/v1/namespaces/.../services/.../proxy/`, the way Grafana is
  reached): Mailpit needs its `--webroot` in the request path, but the proxy strips the prefix before forwarding, so
  the UI's API calls go to the wrong place, and the proxy's HTML link rewriting would add a second prefix. Mailpit also
  rejects a webroot containing `:` (which a `http:name:port` path has). Grafana only works there because it can
  generate prefixed URLs while serving from `/`. Hence the Ingress.
- **Readiness probe with a webroot set** returned 404 and kept the pod unready; irrelevant now (no webroot) but it shows
  the probe path follows the webroot.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| The hostname does not resolve right after you created the route, though `dig @1.1.1.1` answers | A cached "does not exist" in your router or machine (this zone's negative TTL is 30 minutes). Wait, flush the browser/OS cache, or use 1.1.1.1 temporarily. Test the whole path meanwhile with `curl --resolve mailpit.conteudoquestionavel.org:443:104.21.27.71 ...` |
| `401` in the browser or curl | Missing or wrong UI login; read it from Secret `mailpit-ui` |
| Alertmanager logs `establish connection to server` / `Notify retry canceled` | Mailpit was down or restarting. Alertmanager retries; mails resume when it is back. A *firing* notification that exhausted its retries is not re-sent until the group changes |
| Alertmanager logs an SMTP auth failure | `auth_password` in the Alertmanager config differs from Secret `mailpit-smtp` |
| Pod stuck `0/1` | `kubectl describe pod`: a PVC not bound (storage class), or a wrong value in a Secret reference (`mailpit-smtp` / `mailpit-ui` must exist before the Deployment starts) |
| Ingress shows no ADDRESS | Cosmetic on this cluster; requests still work |
| Messages vanished | The PVC was deleted, or more than 500 mails arrived (oldest are dropped) |

## What was verified on the lab

- Without credentials the SMTP port answers `530 authentication required`, a wrong password or user `535`.
- Through the VIP with the real hostname: no or wrong web credentials get `401`; the right ones get the page, its
  assets and the API (`200`).
- Through Cloudflare (public address): `401` without credentials, `200` and the API with them; the certificate browsers
  see is Cloudflare's.
- Deleting the pod keeps the mails (the new pod started after the messages were created and still lists them).
  While Mailpit was down Alertmanager retried and delivered the next notification (the *resolved* mail of the test alert)
  once it was back; the first *firing* mail had already used up its 16 retries and was lost.
- A real `HarvesterVMLauncherMemoryNearLimit` alert (critical) arrived as a mail and renders correctly.
