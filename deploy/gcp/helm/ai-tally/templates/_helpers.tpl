{{/* Chart base name, overridable. */}}
{{- define "ai-tally.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Fully qualified release name. */}}
{{- define "ai-tally.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "ai-tally.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Common labels. */}}
{{- define "ai-tally.labels" -}}
helm.sh/chart: {{ include "ai-tally.chart" . }}
app.kubernetes.io/part-of: ai-tally
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/* Per-tier selector labels. Pass a dict {"ctx": ., "tier": "gateway"}. */}}
{{- define "ai-tally.selectorLabels" -}}
app.kubernetes.io/name: {{ include "ai-tally.name" .ctx }}
app.kubernetes.io/instance: {{ .ctx.Release.Name }}
app.kubernetes.io/component: {{ .tier }}
{{- end -}}

{{/* Tier-suffixed resource name, e.g. "myrel-ai-tally-gateway". */}}
{{- define "ai-tally.tierName" -}}
{{- printf "%s-%s" (include "ai-tally.fullname" .ctx) .tier | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* KSA name the pods run as. */}}
{{- define "ai-tally.serviceAccountName" -}}
{{- default (include "ai-tally.fullname" .) .Values.serviceAccount.name -}}
{{- end -}}

{{/* Name of the Kubernetes Secret the app tiers read env from: an existing one if provided, else
     the one the CSI driver syncs (named after the release). */}}
{{- define "ai-tally.secretName" -}}
{{- if .Values.secretManager.existingSecretName -}}
{{- .Values.secretManager.existingSecretName -}}
{{- else -}}
{{- printf "%s-secrets" (include "ai-tally.fullname" .) -}}
{{- end -}}
{{- end -}}

{{/* SecretProviderClass name (CSI mode). */}}
{{- define "ai-tally.spcName" -}}
{{- printf "%s-spc" (include "ai-tally.fullname" .) -}}
{{- end -}}

{{/* In-cluster ClickHouse Service DNS (statefulset mode). */}}
{{- define "ai-tally.clickhouseHost" -}}
{{- if .Values.clickhouse.host -}}
{{- .Values.clickhouse.host -}}
{{- else if eq .Values.clickhouse.mode "statefulset" -}}
{{- printf "%s-clickhouse" (include "ai-tally.fullname" .) -}}
{{- else -}}
{{- fail "clickhouse.host is required when clickhouse.mode=external" -}}
{{- end -}}
{{- end -}}

{{/* Gateway in-cluster URL the web tier calls. */}}
{{- define "ai-tally.gatewayUrl" -}}
{{- if .Values.web.config.gatewayUrl -}}
{{- .Values.web.config.gatewayUrl -}}
{{- else -}}
{{- printf "http://%s:%d" (include "ai-tally.tierName" (dict "ctx" . "tier" "gateway")) (int .Values.gateway.service.port) -}}
{{- end -}}
{{- end -}}
