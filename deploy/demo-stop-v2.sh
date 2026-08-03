#!/bin/bash
###############################################################
#  데모 환경 종료 스크립트
#  원본: prototype-y1/demo-stop.sh (Apache-2.0, 사내 1차년도 산출물)
#  수정 요약: 내용 동일 (EAM_DIR 등 외부 의존이 없어 수정할 것이 없음) - v2 이름만
#             demo-setup-v2.sh와 짝을 맞춰 유지.
###############################################################

echo ""
echo "KubeEdge 데모 환경을 종료합니다."
echo ""

read -p "VM을 삭제할까요? (y/n): " confirm
if [[ "$confirm" == "y" || "$confirm" == "Y" ]]; then
  echo "VM 중지 및 삭제 중..."
  for vm in cloud edge1 edge2; do
    multipass stop $vm 2>/dev/null
    multipass delete $vm 2>/dev/null
  done
  multipass purge 2>/dev/null
  echo "모든 VM이 삭제되었습니다."
else
  echo "VM을 중지만 합니다. (나중에 multipass start로 재시작 가능)"
  for vm in cloud edge1 edge2; do
    multipass stop $vm 2>/dev/null
  done
  echo "VM이 중지되었습니다."
  echo "   재시작: multipass start cloud edge1 edge2"
fi
