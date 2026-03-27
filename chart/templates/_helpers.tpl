{{/*
Expand the name of the chart.
*/}}
{{- define "sre-agent.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Fullname — release + chart name.
*/}}
{{- define "sre-agent.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Common labels.
*/}}
{{- define "sre-agent.labels" -}}
app.kubernetes.io/name: {{ include "sre-agent.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Selector labels.
*/}}
{{- define "sre-agent.selectorLabels" -}}
app.kubernetes.io/name: {{ include "sre-agent.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
ServiceAccount name.
*/}}
{{- define "sre-agent.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "sre-agent.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Validate AI backend credentials — fail at install time if missing.
*/}}
{{- define "sre-agent.validateCredentials" -}}
{{- if and (not .Values.vertexAI.projectId) (not .Values.anthropicApiKey.existingSecret) }}
{{- fail "AI backend not configured. Set vertexAI.projectId (for Vertex AI) or anthropicApiKey.existingSecret (for Anthropic API). See values.yaml for details." }}
{{- end }}
{{- end }}

{{/*
WS auth token secret name — auto-generates a secret if wsAuth.existingSecret is not set.
*/}}
{{- define "sre-agent.wsTokenSecretName" -}}
{{- if .Values.wsAuth.existingSecret }}
{{- .Values.wsAuth.existingSecret }}
{{- else }}
{{- printf "%s-ws-token" (include "sre-agent.fullname" .) }}
{{- end }}
{{- end }}
