#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<USAGE
Usage:
  sudo ./kubeedge_k8s_full_reset.sh --runtime containerd|docker|both [--flush-nft]

Examples:
  sudo ./kubeedge_k8s_full_reset.sh --runtime containerd
  sudo ./kubeedge_k8s_full_reset.sh --runtime both --flush-nft
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
if [[ "${1:-}" == "--flush-nft" ]]; then
  DO_FLUSH_NFT="true"
fi

if [[ "$RUNTIME" != "containerd" && "$RUNTIME" != "docker" && "$RUNTIME" != "both" ]]; then
  usage
  exit 1
fi

log(){ echo -e "[+] $*"; }

log "0) Stop kubeedge services (if exist)"
systemctl stop cloudcore 2>/dev/null || true
systemctl stop edgecore 2>/dev/null || true
pkill -f cloudcore 2>/dev/null || true
pkill -f edgecore 2>/dev/null || true

log "0) keadm reset (if keadm exists)"
if command -v keadm >/dev/null 2>&1; then
  # Edge/Cloud 모두에서 동작하도록 일단 기본 reset 시도
  keadm reset 2>/dev/null || true

  # 일부 버전은 서브커맨드로 분리됨
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

log "2) Remove k8s/CNI/network state directories"
rm -rf /etc/kubernetes 2>/dev/null || true
rm -rf /var/lib/etcd 2>/dev/null || true
rm -rf /var/lib/kubelet 2>/dev/null || true
rm -rf /etc/cni/net.d /var/lib/cni 2>/dev/null || true
rm -rf /var/lib/calico /var/lib/flannel 2>/dev/null || true

log "2) Remove kubeconfigs"
rm -rf /root/.kube 2>/dev/null || true
if [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
  rm -rf "/home/${SUDO_USER}/.kube" 2>/dev/null || true
fi

log "3) Remove common CNI interfaces"
ip link del cni0 2>/dev/null || true
ip link del flannel.1 2>/dev/null || true
ip link del kube-ipvs0 2>/dev/null || true

ip link del tunl0 2>/dev/null || true
ip link del vxlan.calico 2>/dev/null || true
ip link del wireguard.cali 2>/dev/null || true

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
echo "  - Apply CNI (flannel/calico)"
echo "  - Then keadm init/join again if needed"
