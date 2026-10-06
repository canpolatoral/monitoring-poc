# Pinned versions. Verified current and mutually compatible on 2026-10-06.
# Istio/Kiali follow OpenShift Service Mesh 3.4 (Istio 1.30, Kiali 2.27) so that
# manifests behave the same when moved to OpenShift.
# (Keep comments on their own lines: a trailing comment leaves spaces in the value.)

KIND_VERSION := v0.33.0
# Istio 1.30 supports Kubernetes 1.32-1.36, so use the newest 1.36 image, not kind's 1.37 default.
KIND_NODE_IMAGE := kindest/node:v1.36.4@sha256:099e049362a1526b2db71494e1947aae99bd16290d7c895f2b7ea312e3cbfaed
KUBECTL_VERSION := v1.36.5
HELM_VERSION := v3.22.0
ISTIO_VERSION := 1.30.5

# Helm charts
# Sail Operator 1.31.1 manages Istio v1.29 to v1.31; we run v$(ISTIO_VERSION).
SAIL_CHART_VERSION := 1.31.1
# Same Kiali as OSSM 3.4.
KIALI_CHART_VERSION := 2.27.0
# kube-prometheus-stack (Prometheus Operator v0.94.1)
KPS_CHART_VERSION := 91.9.0
CERT_MANAGER_VERSION := v1.21.2
# OpenTelemetry Operator 0.160.0
OTEL_OPERATOR_CHART_VERSION := 0.124.1
# Loki 3.6.12
LOKI_CHART_VERSION := 7.3.0
# Tempo Operator has no official Helm chart; installed from its pinned release manifest.
TEMPO_OPERATOR_VERSION := v0.22.0
