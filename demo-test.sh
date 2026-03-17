#!/bin/bash
###############################################################
#  Edge 노드 간 통신 테스트 스크립트
#  - edge1 → edge2 메시지 전송
#  - edge2 → edge1 응답 전송
###############################################################
export MSYS_NO_PATHCONV=1

CLOUD_IP=$(multipass info cloud --format csv | tail -1 | cut -d, -f3)

green() { echo -e "\033[32m$*\033[0m"; }
blue()  { echo -e "\033[34m$*\033[0m"; }
red()   { echo -e "\033[31m$*\033[0m"; }

echo ""
blue "=========================================="
blue "  Edge 노드 간 통신 테스트"
blue "=========================================="
echo ""

# 1) 노드 상태 확인
blue "[1/4] 노드 상태 확인"
multipass exec cloud -- kubectl get nodes
echo ""

# 2) Pod 상태 확인
blue "[2/4] Pod 상태 확인"
multipass exec cloud -- kubectl get pods -n edge-auth -o wide
echo ""

# 3) 통신 테스트 Job 실행
blue "[3/4] 통신 테스트 실행 (edge1 ↔ edge2)"

# RabbitMQ 권한 확보
multipass exec cloud -- curl -s -u isl:wjdqhqhghdusrntlf1! -X PUT \
  "http://localhost:15672/api/permissions/%2F/isl" \
  -H "content-type: application/json" \
  -d '{"configure":".*","write":".*","read":".*"}' >/dev/null 2>&1

# 이전 Job 정리
multipass exec cloud -- kubectl delete job comm-edge1-sender comm-edge2-receiver -n edge-auth 2>/dev/null
multipass exec cloud -- kubectl delete configmap comm-test-script -n edge-auth 2>/dev/null
sleep 2

# 테스트 배포 (IP 치환 후 전송)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd -W 2>/dev/null || pwd)"
mkdir -p /tmp/eam-k8s
sed "s/172\.18\.78\.12/$CLOUD_IP/g" "${SCRIPT_DIR}/k8s/test-comm.yaml" > /tmp/eam-k8s/test-comm.yaml
multipass transfer "$(cygpath -w /tmp/eam-k8s/test-comm.yaml 2>/dev/null || echo /tmp/eam-k8s/test-comm.yaml)" cloud:/tmp/test-comm.yaml 2>/dev/null || \
echo "  (configmap이 이미 존재하면 무시 가능)"
multipass exec cloud -- kubectl apply -f /tmp/test-comm.yaml >/dev/null
echo "  테스트 Pod 시작됨. 결과 대기 중 (약 30초)..."
sleep 30

# 4) 결과 출력
blue "[4/4] 테스트 결과"
echo ""
echo "--- edge1 (sender) 로그 ---"
SENDER_POD=$(multipass exec cloud -- kubectl get pods -n edge-auth -l job-name=comm-edge1-sender -o name | head -1)
SENDER_NAME=$(echo $SENDER_POD | sed 's|pod/||')
multipass exec edge1 -- sudo bash -c "cat /var/log/pods/edge-auth_${SENDER_NAME}*/sender/0.log" 2>/dev/null | sed 's/^[^ ]* [^ ]* F //'

echo ""
echo "--- edge2 (receiver) 로그 ---"
RECV_POD=$(multipass exec cloud -- kubectl get pods -n edge-auth -l job-name=comm-edge2-receiver -o name | head -1)
RECV_NAME=$(echo $RECV_POD | sed 's|pod/||')
multipass exec edge2 -- sudo bash -c "cat /var/log/pods/edge-auth_${RECV_NAME}*/receiver/0.log" 2>/dev/null | sed 's/^[^ ]* [^ ]* F //'

echo ""
# Job 상태 확인
SENDER_STATUS=$(multipass exec cloud -- kubectl get $SENDER_POD -n edge-auth -o jsonpath='{.status.phase}' 2>/dev/null)
RECV_STATUS=$(multipass exec cloud -- kubectl get $RECV_POD -n edge-auth -o jsonpath='{.status.phase}' 2>/dev/null)

echo ""
blue "=========================================="
if [[ "$SENDER_STATUS" == "Succeeded" && "$RECV_STATUS" == "Succeeded" ]]; then
  green "  ✅ 통신 테스트 성공!"
  green "  edge1 → edge2 메시지 전송 OK"
  green "  edge2 → edge1 응답 전송 OK"
else
  red "  ⚠ 테스트 상태: sender=$SENDER_STATUS, receiver=$RECV_STATUS"
  red "  Pod가 아직 실행 중이면 잠시 후 다시 실행하세요."
fi
blue "=========================================="
echo ""
echo "  📊 Dashboard에서 확인: http://$CLOUD_IP:30501"
echo "  🐰 RabbitMQ 관리:     http://$CLOUD_IP:15672"
