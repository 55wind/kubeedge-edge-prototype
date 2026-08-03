#!/bin/bash
###############################################################
#  eam-manager:v2 / eam-agent:v2 이미지 빌드
#  - 신규 작성 (1차년도 demo-setup.sh의 "6) 이미지 빌드" 단계를 참고했으나,
#    1차년도는 EAM_DIR(별도 프로토타입 저장소) 소스를 tar로 옮겨 edge1 VM
#    안에서 nerdctl로 빌드했다. 본 repo는 소스를 자체적으로 포함하므로 그
#    복잡한 전송 단계 없이 로컬 docker build만으로 충분하다.
#  - Multipass VM(cloud/edge1/edge2) 안에서 이미지를 쓰려면 이 스크립트가
#    만든 이미지를 `docker save`/`multipass transfer`/`ctr images import`로
#    옮기거나, demo-setup-v2.sh가 하듯 VM 내부에서 직접 이 스크립트를
#    실행하면 된다.
###############################################################
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

MANAGER_IMAGE="${MANAGER_IMAGE:-eam-manager:v2}"
AGENT_IMAGE="${AGENT_IMAGE:-eam-agent:v2}"

if ! command -v docker &>/dev/null; then
  echo "docker 명령을 찾을 수 없습니다. Docker(또는 nerdctl 별칭)를 설치하세요." >&2
  exit 1
fi

echo "[1/2] ${MANAGER_IMAGE} 빌드 중..."
docker build -f "$SCRIPT_DIR/Dockerfile.manager" -t "$MANAGER_IMAGE" "$REPO_ROOT"

echo "[2/2] ${AGENT_IMAGE} 빌드 중..."
docker build -f "$SCRIPT_DIR/Dockerfile.agent" -t "$AGENT_IMAGE" "$REPO_ROOT"

echo ""
echo "빌드 완료:"
echo "  - ${MANAGER_IMAGE}"
echo "  - ${AGENT_IMAGE} (agent-edge1/agent-edge2/gateway 매니페스트가 공유)"
