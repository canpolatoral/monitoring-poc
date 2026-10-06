#!/usr/bin/env bash
# Download pinned CLI tools into .tools/bin so nothing global is changed.
set -euo pipefail
BIN="$TOOLS_DIR/bin"; mkdir -p "$BIN"

os=$(uname -s | tr '[:upper:]' '[:lower:]')          # darwin | linux
arch=$(uname -m); case "$arch" in x86_64) arch=amd64;; aarch64|arm64) arch=arm64;; esac

have() { [ -x "$BIN/$1" ] && "$BIN/$1" $2 2>/dev/null | grep -q -- "$3"; }

if ! have kind version "$KIND_VERSION"; then
  echo ">> kind $KIND_VERSION"
  curl -fsSLo "$BIN/kind" "https://kind.sigs.k8s.io/dl/$KIND_VERSION/kind-$os-$arch" && chmod +x "$BIN/kind"
fi
if ! have kubectl "version --client" "$KUBECTL_VERSION"; then
  echo ">> kubectl $KUBECTL_VERSION"
  curl -fsSLo "$BIN/kubectl" "https://dl.k8s.io/release/$KUBECTL_VERSION/bin/$os/$arch/kubectl" && chmod +x "$BIN/kubectl"
fi
if ! have helm version "$HELM_VERSION"; then
  echo ">> helm $HELM_VERSION"
  curl -fsSL "https://get.helm.sh/helm-$HELM_VERSION-$os-$arch.tar.gz" | tar -xz -C "$BIN" --strip-components=1 "$os-$arch/helm"
fi
if ! have istioctl "version --remote=false" "$ISTIO_VERSION"; then
  echo ">> istioctl $ISTIO_VERSION"
  iarch=$arch; [ "$os" = darwin ] && ios=osx || ios=linux
  curl -fsSL "https://github.com/istio/istio/releases/download/$ISTIO_VERSION/istioctl-$ISTIO_VERSION-$ios-$iarch.tar.gz" | tar -xz -C "$BIN"
fi
echo "tools ready in $BIN:"
echo "  $("$BIN/kind" version)"
echo "  kubectl $("$BIN/kubectl" version --client -o json | grep -m1 gitVersion | tr -d ' ",')"
echo "  helm $("$BIN/helm" version --short)"
echo "  istioctl $("$BIN/istioctl" version --remote=false 2>/dev/null)"
