{{/*
Chart name and version label
*/}}
{{- define "harvester-extra-dashboards.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Common labels
*/}}
{{- define "harvester-extra-dashboards.labels" -}}
helm.sh/chart: {{ include "harvester-extra-dashboards.chart" . }}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: harvester-extra-dashboards
{{- end -}}

{{/*
Directory (inside the chart) holding the dashboard JSON of the selected variant
*/}}
{{- define "harvester-extra-dashboards.dir" -}}
{{- ternary "dashboards-psi" "dashboards" .Values.psi.enabled -}}
{{- end -}}
