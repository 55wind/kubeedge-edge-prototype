#!/usr/bin/env bash
# Cloud(Master) 노드 원클릭 셋업
# 사용법: sudo ./run-cloud-setup.sh
#
# README 순서:
#   1. setup-cloud.sh (containerd/crictl/cni/keadm 설치)
#   2. kubeadm init
#   3. keadm init + patch-cloud.sh
#   4. install-flannel-cloud.sh
#   5. cloudcore control-plane 고정 패치

set -o errexit
set -o nounset
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

source ./env.sh

if [[ "$(id -u)" -ne 0 ]]; then
    echo "[ERR] root 권한 필요: sudo ./run-cloud-setup.sh"
    exit 1
fi

log() { echo -e "\n===== [$1] $2 =====\n"; }

# -------------------------------------------------------
# Step 1: 기본 패키지 설치 (containerd, crictl, cni, keadm)
# -------------------------------------------------------
log "1/6" "setup-cloud.sh 실행 (기본 패키지 설치)"
./setup-cloud.sh

# -------------------------------------------------------
# Step 2: kubeadm init (이미 초기화되어 있으면 스킵)
# -------------------------------------------------------
log "2/6" "kubeadm init"
if kubectl get nodes >/dev/null 2>&1; then
    echo "kubeadm 이미 초기화됨 -> 스킵"
else
    kubeadm init \
        --apiserver-advertise-address="$CLOUD_IP" \
        --pod-network-cidr="$POD_NETWORK_CIDR"

    # kubeconfig 설정
    REAL_USER="${SUDO_USER:-root}"
    REAL_HOME=$(eval echo "~$REAL_USER")
    mkdir -p "$REAL_HOME/.kube"
    cp /etc/kubernetes/admin.conf "$REAL_HOME/.kube/config"
    chown "$(id -u "$REAL_USER"):$(id -g "$REAL_USER")" "$REAL_HOME/.kube/config"
    export KUBECONFIG="$REAL_HOME/.kube/config"
fi

# KUBECONFIG 설정 (이후 kubectl 명령 사용을 위해)
if [[ -z "${KUBECONFIG:-}" ]]; then
    if [[ -f /etc/kubernetes/admin.conf ]]; then
        export KUBECONFIG=/etc/kubernetes/admin.conf
    fi
fi

# -------------------------------------------------------
# Step 3: 마스터 테인트 제거
# -------------------------------------------------------
log "3/6" "마스터 테인트 제거"
kubectl taint nodes --all node-role.kubernetes.io/control-plane- 2>/dev/null || true
kubectl taint nodes --all node-role.kubernetes.io/master- 2>/dev/null || true

# -------------------------------------------------------
# Step 4: CloudCore 초기화 + 패치
# -------------------------------------------------------
log "4/6" "keadm init + patch-cloud.sh"
keadm init --advertise-address="$CLOUD_IP" --kubeedge-version="${KUBEEDGE_VERSION#v}"
./patch-cloud.sh

# -------------------------------------------------------
# Step 5: Flannel (Cloud) 설치
# -------------------------------------------------------
log "5/6" "install-flannel-cloud.sh"
./install-flannel-cloud.sh

# -------------------------------------------------------
# Step 6: cloudcore를 control-plane에 고정
# -------------------------------------------------------
log "6/6" "cloudcore control-plane 노드 고정 패치"
kubectl -n kubeedge patch deploy cloudcore --type='merge' -p '{
  "spec": {
    "template": {
      "spec": {
        "nodeSelector": {
          "node-role.kubernetes.io/control-plane": ""
        },
        "tolerations": [
          {
            "key": "node-role.kubernetes.io/control-plane",
            "operator": "Exists",
            "effect": "NoSchedule"
          }
        ]
      }
    }
  }
}'

# -------------------------------------------------------
# 결과 확인
# -------------------------------------------------------
echo ""
echo "=========================================="
echo " Cloud 노드 셋업 완료!"
echo "=========================================="
echo ""
kubectl get nodes -o wide
echo ""
kubectl get pods -A -o wide
echo ""
echo "Edge 토큰 발급:"
keadm gettoken
echo ""
echo "다음 단계: Edge 노드에서 run-edge-setup.sh 실행"
