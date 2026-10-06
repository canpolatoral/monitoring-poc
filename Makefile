# E2E observability POC on kind. Run `make help` for targets.
include versions.mk

CLUSTER   := bank-poc
TOOLS_DIR := $(CURDIR)/.tools
SHELL     := /bin/bash

# Everything runs with project-local tools, kubeconfig and Helm config; nothing global is touched.
export PATH       := $(TOOLS_DIR)/bin:$(PATH)
export KUBECONFIG := $(TOOLS_DIR)/kubeconfig
export HELM_REPOSITORY_CONFIG := $(TOOLS_DIR)/helm/repositories.yaml
export HELM_CACHE_HOME        := $(TOOLS_DIR)/helm/cache
export HELM_CONFIG_HOME       := $(TOOLS_DIR)/helm/config
export HELM_DATA_HOME         := $(TOOLS_DIR)/helm/data
.EXPORT_ALL_VARIABLES:

.PHONY: urls help up down tools cluster mesh apps metrics tracing externals dashboard demo-new-service remove-new-service hops status env demo-core-slow demo-db-down demo-switch-down reset fault-status grafana-password

help: ## Show targets
	@grep -hE '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

up: tools cluster mesh apps metrics tracing externals dashboard ## Create everything (idempotent)
	@$(MAKE) -s urls

urls: ## Print all local URLs
	@echo; echo "  Red-dot dashboard  http://localhost:18088"
	@echo "  Kiali              http://localhost:20001/kiali"
	@echo "  Grafana            http://localhost:13000   (anonymous viewer; admin password: make grafana-password)"
	@echo "                     journey dashboard: http://localhost:13000/d/e2e-journey"
	@echo "  Prometheus         http://localhost:19090"
	@echo "  Alertmanager       http://localhost:19093"
	@echo "  Tempo API          http://localhost:13200   (browse traces in Grafana > Explore > Tempo)"
	@echo "  Bank API           http://localhost:18080/api/{login,balance,transfers,cards/payments}"

down: ## Delete the kind cluster and the external mock containers
	-kind delete cluster --name $(CLUSTER)
	-docker rm -f $$(docker ps -aq --filter label=com.bank-poc=external) 2>/dev/null

tools: ## Download pinned kind/kubectl/helm/istioctl into .tools/bin
	@scripts/tools.sh

cluster: ## Create the kind cluster and Helm repos
	@scripts/cluster.sh

mesh: ## Sail Operator, Istio + CNI, ingress gateway
	@scripts/mesh.sh

apps: ## Build and deploy the sample bank services and the load generator
	@scripts/apps.sh

metrics: ## Prometheus, Alertmanager, Grafana (kube-prometheus-stack), Istio scraping, Kiali
	@scripts/metrics.sh

tracing: ## Tempo, Loki, OTel Operator + Collector, Istio telemetry, journey tagging
	@scripts/tracing.sh

externals: ## External systems as containers (core banking, card switch, DB, SMS) + ServiceEntries
	@scripts/externals.sh

dashboard: ## Red-dot dashboard (backend + live UI) and the Grafana journey dashboard
	@scripts/dashboard.sh

demo-new-service: ## Add loans-svc + credit bureau + loan journey; the dashboard discovers them
	@scripts/new-service.sh add

remove-new-service: ## Remove the loans demo add-on
	@scripts/new-service.sh remove

demo-core-slow: ## Fault: core banking answers in 1-4 s (timeouts at 3 s -> UT)
	@scripts/fault.sh core-slow

demo-db-down: ## Fault: database refuses connections (UF)
	@scripts/fault.sh db-down

demo-switch-down: ## Fault: card switch down (UF, then UH after ejection)
	@scripts/fault.sh switch-down

reset: ## Clear all injected faults
	@scripts/fault.sh reset

fault-status: ## Show the fault state of every mock
	@scripts/fault.sh status

grafana-password: ## Print the generated Grafana admin password
	@kubectl get secret grafana-admin -n monitoring -o jsonpath='{.data.admin-password}' | base64 -d; echo

hops: ## Per-hop req/s, 5xx %, p95 and Envoy flags over the last minute
	@scripts/hops.sh

status: ## Show pods and endpoints
	@kubectl get pods -A -o wide | grep -vE "kube-system|local-path" || true

env: ## Print exports to use kubectl/istioctl from your shell: eval "$(make -s env)"
	@echo "export PATH=$(TOOLS_DIR)/bin:\$$PATH KUBECONFIG=$(KUBECONFIG)"
