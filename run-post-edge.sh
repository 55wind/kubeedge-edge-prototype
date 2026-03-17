#!/usr/bin/env bash
# Edge 조인 후 Cloud에서 실행하는 후처리 스크립트
# 사용법: sudo ./run-post-edge.sh
#
# README 순서:
#   1. install-flannel-edge.sh (Cloud에서 실행)
#   2. EdgeMesh 설치
#   3. kube-proxy edge 배치 방지

set -o errexit
set -o nounset
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

source ./env.sh

if [[ "$(id -u)" -ne 0 ]]; then
    echo "[ERR] root 권한 필요: sudo ./run-post-edge.sh"
    exit 1
fi

if [[ -z "${KUBECONFIG:-}" && -f /etc/kubernetes/admin.conf ]]; then
    export KUBECONFIG=/etc/kubernetes/admin.conf
fi

log() { echo -e "\n===== [$1] $2 =====\n"; }

# -------------------------------------------------------
# Step 1: Flannel (Edge) 배포
# -------------------------------------------------------
log "1/3" "install-flannel-edge.sh"
./install-flannel-edge.sh

# -------------------------------------------------------
# Step 2: kube-proxy edge 배치 방지
# -------------------------------------------------------
log "2/3" "kube-proxy edge 노드 제외 패치"
kubectl patch daemonset kube-proxy -n kube-system -p '{"spec": {"template": {"spec": {"affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [{"matchExpressions": [{"key": "node-role.kubernetes.io/edge", "operator": "DoesNotExist"}]}]}}}}}}}'

# -------------------------------------------------------
# Step 3: EdgeMesh 설치
# -------------------------------------------------------
log "3/3" "EdgeMesh 설치"

MASTER_HOSTNAME="$(hostname)"

if ! command -v helm >/dev/null 2>&1; then
    echo "[WARN] helm이 설치되어 있지 않습니다."
    echo "helm 설치 후 아래 명령어를 수동으로 실행하세요:"
    echo ""
    echo "  helm repo add edgemesh https://kubeedge.io/edgemesh"
    echo "  helm repo update"
    echo "  PSK=\$(openssl rand -base64 32)"
    echo "  helm install edgemesh --namespace kubeedge \\"
    echo "    --set agent.psk=\"\$PSK\" \\"
    echo "    --set agent.relayNodes[0].nodeName=${MASTER_HOSTNAME} \\"
    echo "    --set agent.relayNodes[0].advertiseAddress=\"{${CLOUD_IP}}\" \\"
    echo "    edgemesh/edgemesh"
else
    helm repo add edgemesh https://kubeedge.io/edgemesh 2>/dev/null || true
    helm repo update

    PSK=$(openssl rand -base64 32)
    helm install edgemesh --namespace kubeedge \
        --set agent.psk="$PSK" \
        --set "agent.relayNodes[0].nodeName=${MASTER_HOSTNAME}" \
        --set "agent.relayNodes[0].advertiseAddress={${CLOUD_IP}}" \
        edgemesh/edgemesh
fi

# -------------------------------------------------------
# 결과 확인
# -------------------------------------------------------
echo ""
echo "=========================================="
echo " 후처리 완료!"
echo "=========================================="
echo ""
kubectl get nodes -o wide
echo ""
kubectl get pods -A -o wide
echo ""
kubectl -n kubeedge get pods -o wide | grep edgemesh || true
echo ""
kubectl -n kube-system get ds kube-proxy -o wide
echo ""
echo "릴레이 노드 이름 확인 (마스터 호스트명과 일치해야 함):"
echo "  kubectl edit configmap edgemesh-agent-cfg -n kubeedge"
