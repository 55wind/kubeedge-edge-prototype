#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  sudo ./kubeedge_k8s_full_reset.sh --runtime containerd|docker|both
                                   [--flush-nft]
                                   [--cleanup-calico-k8s]
                                   [--cleanup-calico-crd]

Examples:
  sudo ./kubeedge_k8s_full_reset.sh --runtime containerd
  sudo ./kubeedge_k8s_full_reset.sh --runtime both --flush-nft
  sudo ./kubeedge_k8s_full_reset.sh --runtime containerd --cleanup-calico-k8s
  sudo ./kubeedge_k8s_full_reset.sh --runtime containerd --cleanup-calico-k8s --cleanup-calico-crd
USAGE
}

if [[ $EUID -ne 0 ]]; then
  echo "[ERR] Run as root (use sudo)."
  exit 1
fi

if [[ "${1:-}" != "--runtime" ]]; then
  usage
  exit 1
fi

RUNTIME="${2:-}"
shift 2 || true

DO_FLUSH_NFT="false"
DO_CLEANUP_CALICO_K8S="false"
DO_CLEANUP_CALICO_CRD="false"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --flush-nft) DO_FLUSH_NFT="true" ;;
    --cleanup-calico-k8s) DO_CLEANUP_CALICO_K8S="true" ;;
    --cleanup-calico-crd) DO_CLEANUP_CALICO_CRD="true" ;;
    *) echo "[ERR] Unknown option: $1"; usage; exit 1 ;;
  esac
  shift
done

if [[ "$RUNTIME" != "containerd" && "$RUNTIME" != "docker" && "$RUNTIME" != "both" ]]; then
  usage
  exit 1
fi

log(){ echo -e "[+] $*"; }

# ----------------------------
# (Optional) Calico cleanup in cluster (control-plane only, best effort)
# ----------------------------
cleanup_calico_k8s_resources() {
  if ! command -v kubectl >/dev/null 2>&1; then
    log "kubectl not found -> skip cluster calico cleanup"
    return 0
  fi

  # Try to see if we even can talk to the API; if not, skip.
  if ! kubectl version --request-timeout=3s >/dev/null 2>&1; then
    log "kubectl cannot reach apiserver -> skip cluster calico cleanup"
    return 0
  fi

  log "2.x) Cleanup Calico/Tigera namespaces (best effort)"
  kubectl delete ns calico-system 2>/dev/null || true
  kubectl delete ns tigera-operator 2>/dev/null || true

  # Sometimes calico components are installed into kube-system with labels
  log "2.x) Cleanup Calico/Tigera workloads in kube-system (best effort)"
  kubectl -n kube-system delete ds -l k8s-app=calico-node 2>/dev/null || true
  kubectl -n kube-system delete ds -l k8s-app=calico-kube-controllers 2>/dev/null || true
  kubectl -n kube-system delete deploy -l k8s-app=calico-kube-controllers 2>/dev/null || true

  # NetworkPolicy engine / operator resources (best effort)
  kubectl -n kube-system delete deploy -l k8s-app=tigera-operator 2>/dev/null || true
  kubectl -n kube-system delete deploy -l app=tigera-operator 2>/dev/null || true
}

cleanup_calico_crds() {
  if ! command -v kubectl >/dev/null 2>&1; then
    log "kubectl not found -> skip calico CRD cleanup"
    return 0
  fi
  if ! kubectl version --request-timeout=3s >/dev/null 2>&1; then
    log "kubectl cannot reach apiserver -> skip calico CRD cleanup"
    return 0
  fi

  log "2.x) Cleanup Calico/Tigera CRDs (best effort)"
  # Delete only CRDs clearly owned by calico/tigera (safe-ish but still destructive)
  mapfile -t CRDS < <(kubectl get crd -o name 2>/dev/null | egrep -i '(projectcalico|tigera)\.org' || true)
  if [[ ${#CRDS[@]} -eq 0 ]]; then
    log "No calico/tigera CRDs found -> skip"
    return 0
  fi

  for crd in "${CRDS[@]}"; do
    kubectl delete "$crd" 2>/dev/null || true
  done
}

# ----------------------------
# Start
# ----------------------------
log "0) Stop kubeedge services (if exist)"
systemctl stop cloudcore 2>/dev/null || true
systemctl stop edgecore 2>/dev/null || true
pkill -f cloudcore 2>/dev/null || true
pkill -f edgecore 2>/dev/null || true

log "0) keadm reset (if keadm exists)"
if command -v keadm >/dev/null 2>&1; then
  keadm reset 2>/dev/null || true
  keadm reset edge 2>/dev/null || true
  keadm reset cloud 2>/dev/null || true
else
  log "keadm not found -> skip"
fi

log "0) Force-clean kubeedge dirs"
rm -rf /etc/kubeedge /var/lib/kubeedge /var/log/kubeedge 2>/dev/null || true

log "1) Stop kubelet and container runtime(s)"
systemctl stop kubelet 2>/dev/null || true
if [[ "$RUNTIME" == "containerd" || "$RUNTIME" == "both" ]]; then
  systemctl stop containerd 2>/dev/null || true
fi
if [[ "$RUNTIME" == "docker" || "$RUNTIME" == "both" ]]; then
  systemctl stop docker 2>/dev/null || true
fi

log "1) kubeadm reset"
kubeadm reset -f 2>/dev/null || true

# Optional: clean Calico/Tigera cluster resources BEFORE deleting kubeconfigs (best effort)
if [[ "$DO_CLEANUP_CALICO_K8S" == "true" ]]; then
  cleanup_calico_k8s_resources
fi
if [[ "$DO_CLEANUP_CALICO_CRD" == "true" ]]; then
  cleanup_calico_crds
fi

log "2) Remove k8s/CNI/network state directories"
rm -rf /etc/kubernetes 2>/dev/null || true
rm -rf /var/lib/etcd 2>/dev/null || true
rm -rf /var/lib/kubelet 2>/dev/null || true

# Remove CNI configs/state
rm -rf /etc/cni/net.d /var/lib/cni 2>/dev/null || true
rm -rf /var/lib/calico /var/lib/flannel 2>/dev/null || true

log "2) (Extra) Remove Calico/Tigera CNI remnants if any"
rm -f /etc/cni/net.d/*calico* /etc/cni/net.d/*tigera* /etc/cni/net.d/*cali* 2>/dev/null || true

log "2) Remove kubeconfigs"
rm -rf /root/.kube 2>/dev/null || true
if [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
  rm -rf "/home/${SUDO_USER}/.kube" 2>/dev/null || true
fi

log "3) Remove common CNI interfaces"
ip link del cni0 2>/dev/null || true
ip link del flannel.1 2>/dev/null || true
ip link del kube-ipvs0 2>/dev/null || true

# Calico remnants
ip link del tunl0 2>/dev/null || true
ip link del vxlan.calico 2>/dev/null || true
ip link del wireguard.cali 2>/dev/null || true

# Remaining cali*/veth* (best effort)
for i in $(ip -o link show | awk -F': ' '{print $2}' | grep -E '^(cali|veth)' || true); do
  ip link del "$i" 2>/dev/null || true
done

log "4) Flush iptables/ip6tables rules"
iptables -F 2>/dev/null || true
iptables -t nat -F 2>/dev/null || true
iptables -t mangle -F 2>/dev/null || true
iptables -t raw -F 2>/dev/null || true
iptables -X 2>/dev/null || true

ip6tables -F 2>/dev/null || true
ip6tables -t nat -F 2>/dev/null || true
ip6tables -t mangle -F 2>/dev/null || true
ip6tables -t raw -F 2>/dev/null || true
ip6tables -X 2>/dev/null || true

log "4) Clear IPVS (if exists)"
if command -v ipvsadm >/dev/null 2>&1; then
  ipvsadm --clear 2>/dev/null || true
fi

if [[ "$DO_FLUSH_NFT" == "true" ]]; then
  log "4) Flush nftables ruleset (optional)"
  if command -v nft >/dev/null 2>&1; then
    nft flush ruleset 2>/dev/null || true
  else
    log "nft not found -> skip"
  fi
fi

log "5) Start container runtime(s) and kubelet"
if [[ "$RUNTIME" == "containerd" || "$RUNTIME" == "both" ]]; then
  systemctl start containerd 2>/dev/null || true
fi
if [[ "$RUNTIME" == "docker" || "$RUNTIME" == "both" ]]; then
  systemctl start docker 2>/dev/null || true
fi
systemctl start kubelet 2>/dev/null || true

log "DONE."
echo "Next steps:"
echo "  - Control plane: kubeadm init --apiserver-advertise-address=<IP> --pod-network-cidr=10.244.0.0/16"
echo "  - Apply ONE CNI only (flannel OR calico). Do not mix."
echo "  - Then keadm init/join again if needed"
