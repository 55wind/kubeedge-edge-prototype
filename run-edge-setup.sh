#!/usr/bin/env bash
# Edge 노드 원클릭 셋업
# 사용법: sudo ./run-edge-setup.sh --token <TOKEN>
#
# README 순서:
#   1. setup-edge.sh (crictl/cni/이미지 로드/keadm 설치)
#   2. keadm join
#   3. patch-edge.sh

set -o errexit
set -o nounset
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

source ./env.sh

if [[ "$(id -u)" -ne 0 ]]; then
    echo "[ERR] root 권한 필요: sudo ./run-edge-setup.sh --token <TOKEN>"
    exit 1
fi

TOKEN=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --token)
            TOKEN="$2"
            shift 2
            ;;
        *)
            echo "Unknown option: $1"
            echo "Usage: sudo ./run-edge-setup.sh --token <TOKEN>"
            exit 1
            ;;
    esac
done

if [[ -z "$TOKEN" ]]; then
    echo "[ERR] 토큰이 필요합니다."
    echo "Cloud 노드에서 'keadm gettoken' 으로 토큰을 발급받으세요."
    echo ""
    echo "Usage: sudo ./run-edge-setup.sh --token <TOKEN>"
    exit 1
fi

log() { echo -e "\n===== [$1] $2 =====\n"; }

# -------------------------------------------------------
# Step 1: 기본 패키지 설치 (crictl, cni, 이미지 로드, keadm)
# -------------------------------------------------------
log "1/3" "setup-edge.sh 실행 (기본 패키지 설치)"
./setup-edge.sh

# -------------------------------------------------------
# Step 2: keadm join (Cloud에 조인)
# -------------------------------------------------------
log "2/3" "keadm join (Cloud: ${CLOUD_IP}:10000)"
keadm join \
    --cloudcore-ipport="${CLOUD_IP}:10000" \
    --token="$TOKEN" \
    --kubeedge-version="${KUBEEDGE_VERSION#v}"

# -------------------------------------------------------
# Step 3: Edge 패치 (containerd + metaServer 활성화)
# -------------------------------------------------------
log "3/3" "patch-edge.sh 실행"
./patch-edge.sh

echo ""
echo "=========================================="
echo " Edge 노드 셋업 완료!"
echo "=========================================="
echo ""
echo "Cloud 노드에서 확인:"
echo "  kubectl get nodes -o wide"
echo "  kubectl get pods -A -o wide"
echo ""
echo "다음 단계 (Cloud에서 실행):"
echo "  1. sudo ./install-flannel-edge.sh"
echo "  2. EdgeMesh 설치 (README 4-1 참고)"
