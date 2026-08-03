#!/bin/bash
###############################################################
#  KubeEdge + Edge-Auth-Manager(EAM) 2차년도 데모 환경 원클릭 구축 스크립트
#
#  원본: prototype-y1/demo-setup.sh (Apache-2.0, 사내 1차년도 산출물)
#  수정 요약:
#    (1) EAM_DIR(별도 프로토타입 저장소) 의존 제거 - 본 repo(이 파일 기준
#        상위 디렉터리)의 pyproject.toml/README.md/src를 그대로 사용
#    (2) Secret 생성 단계 신규 추가 - eam-secrets(bootstrap 토큰/관리자·운영자
#        자격증명), rabbitmq-secrets(RabbitMQ 계정), rabbitmq-certs(RabbitMQ
#        전용 자기서명 TLS 인증서)를 매 배포마다 openssl rand로 새로 생성한다.
#        1차년도처럼 정적 파일에 비밀번호를 박아두지 않는다.
#    (3) 이미지 태그 eam-manager:v2 / eam-agent:v2, dashboard 이미지는 빌드하지
#        않음(본 repo에 dashboard 서비스가 없음 - Task 5 범위에서 스킵)
#    (4) RabbitMQ는 선택 사항(optional) 데이터플레인으로 문서화 - 이미지도
#        VM이 Docker Hub에서 직접 pull하도록 두고(에어갭 전송 생략), manifest
#        적용 자체도 필수 단계가 아니라 "9번" 단계에서 안내만 한다.
#    (5) 커널 파라미터/스왑 하드닝(swapoff, ip_forward, br_netfilter) 단계를
#        VM 생성 직후에 추가 - 1차년도 setup-cloud.sh에 있던 조치를 K3s
#        Multipass 플로우에도 명시적으로 적용해 kubelet/CNI 불안정을 예방한다.
#    (6) 나머지(재시도 루프, CrashLoopBackOff 개별 재시작, 대기 시간 등 안정화
#        조치)는 1차년도 그대로 유지.
#
#  - Windows (Git Bash) 에서 실행
#  - 필요: Multipass, Hyper-V 활성화
#  - K3s 기반 (kubeadm 대비 가볍고 안정적)
#  - 실행 검증은 Windows 로컬에 Multipass가 없어 CI에서 수행하지 않는다.
#    `bash -n deploy/demo-setup-v2.sh` 로 문법만 검증한다.
###############################################################
set -e
export MSYS_NO_PATHCONV=1

CLOUD_IP=""
KUBEEDGE_VER="1.19.0"
K3S_VER="v1.29.15+k3s1"
MANAGER_IMAGE="eam-manager:v2"
AGENT_IMAGE="eam-agent:v2"

red()   { echo -e "\033[31m$*\033[0m"; }
green() { echo -e "\033[32m$*\033[0m"; }
blue()  { echo -e "\033[34m$*\033[0m"; }
step()  { echo ""; blue "[$1/$TOTAL_STEPS] $2"; }

TOTAL_STEPS=10

SCRIPT_ROOT="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_ROOT/.." && pwd)"

#----------------------------------------------------------
# 사전 체크
#----------------------------------------------------------
step 0 "사전 요구사항 확인"
if ! command -v multipass &>/dev/null; then
  red "Multipass가 설치되어 있지 않습니다."
  exit 1
fi
green "Multipass 확인됨"

if [ ! -f "$REPO_ROOT/pyproject.toml" ]; then
  red "repo 루트(pyproject.toml)를 찾을 수 없습니다. deploy/ 아래에서 실행하세요."
  exit 1
fi
green "eam repo 소스 확인됨: $REPO_ROOT"

#----------------------------------------------------------
# 1) VM 생성
#----------------------------------------------------------
step 1 "VM 3대 생성 (cloud, edge1, edge2) — 약 2분 소요"
for vm in cloud edge1 edge2; do
  if multipass info $vm &>/dev/null 2>&1; then
    echo "  $vm VM이 이미 존재합니다. 건너뜁니다."
  else
    echo "  $vm VM 생성 중..."
    if [ "$vm" = "cloud" ]; then
      multipass launch 22.04 -n $vm -c 2 -m 4G -d 20G
    else
      multipass launch 22.04 -n $vm -c 2 -m 2G -d 10G
    fi
  fi
done
CLOUD_IP=$(multipass info cloud --format csv | tail -1 | cut -d, -f3)
EDGE1_IP=$(multipass info edge1 --format csv | tail -1 | cut -d, -f3)
EDGE2_IP=$(multipass info edge2 --format csv | tail -1 | cut -d, -f3)
green "cloud=$CLOUD_IP, edge1=$EDGE1_IP, edge2=$EDGE2_IP"

echo "  커널 파라미터/스왑 하드닝 적용 중 (모든 VM)..."
for vm in cloud edge1 edge2; do
  multipass exec $vm -- sudo bash -c "
    swapoff -a || true
    sed -i '/ swap /s/^/#/' /etc/fstab || true
    modprobe br_netfilter || true
    cat > /etc/sysctl.d/99-k8s.conf <<'EOF'
net.ipv4.ip_forward = 1
net.bridge.bridge-nf-call-iptables = 1
vm.swappiness = 0
EOF
    sysctl --system >/dev/null 2>&1 || true
  "
done
green "커널 파라미터/스왑 하드닝 완료"

#----------------------------------------------------------
# 2) Cloud 노드: K3s + CloudCore
#----------------------------------------------------------
step 2 "Cloud 노드 설정 (K3s + KubeEdge CloudCore) — 약 2분"
multipass exec cloud -- bash -c "
  if command -v k3s &>/dev/null; then echo 'K3s already installed'; exit 0; fi
  curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION=${K3S_VER} INSTALL_K3S_EXEC='server' sh -s - \
    --advertise-address=$CLOUD_IP \
    --node-external-ip=$CLOUD_IP \
    --write-kubeconfig-mode=644 \
    --disable=traefik \
    --disable=servicelb \
    --cluster-cidr=10.244.0.0/16 \
    --service-cidr=10.96.0.0/16 \
    --flannel-iface=eth0
  mkdir -p ~/.kube && sudo cp /etc/rancher/k3s/k3s.yaml ~/.kube/config && sudo chown \$(id -u):\$(id -g) ~/.kube/config
  sudo mkdir -p /root/.kube && sudo cp /etc/rancher/k3s/k3s.yaml /root/.kube/config
  echo 'K3s init done'
"

echo "  API 서버 준비 대기 중..."
multipass exec cloud -- bash -c "
  for i in \$(seq 1 60); do kubectl get nodes &>/dev/null && break; echo '  대기 중...'; sleep 5; done
  kubectl taint nodes cloud node-role.kubernetes.io/control-plane:NoSchedule- 2>/dev/null || true
  # K3s는 control-plane=true로 라벨링하지만 매니페스트는 빈 값을 기대 — 호환성 패치
  kubectl label node cloud node-role.kubernetes.io/control-plane='' --overwrite 2>/dev/null || true
"

echo "  CloudCore 설치 중..."
multipass exec cloud -- bash -c "
  if command -v keadm &>/dev/null; then echo 'keadm already installed'; exit 0; fi
  curl -sLO https://github.com/kubeedge/kubeedge/releases/download/v${KUBEEDGE_VER}/keadm-v${KUBEEDGE_VER}-linux-amd64.tar.gz
  tar xzf keadm-v${KUBEEDGE_VER}-linux-amd64.tar.gz
  sudo cp keadm-v${KUBEEDGE_VER}-linux-amd64/keadm/keadm /usr/local/bin/
  sudo keadm init --advertise-address=$CLOUD_IP --kubeedge-version=${KUBEEDGE_VER} --force
"
green "Cloud 노드 설정 완료"

#----------------------------------------------------------
# 3) Token 획득
#----------------------------------------------------------
step 3 "KubeEdge 토큰 획득"
echo "  CloudCore Pod 준비 대기 중 (최대 5분)..."
sleep 60
multipass exec cloud -- bash -c "
  for i in \$(seq 1 60); do kubectl get pods -n kubeedge 2>/dev/null | grep -q '1/1.*Running' && break; sleep 5; done
"
TOKEN=""
for i in $(seq 1 20); do
  RAW=$(multipass exec cloud -- sudo keadm gettoken 2>/dev/null || true)
  if echo "$RAW" | grep -qE '^[a-zA-Z0-9.]{100,}$'; then
    TOKEN="$RAW"
    break
  fi
  if echo "$RAW" | grep -q 'tokensecret.*not found'; then
    echo "  tokensecret 없음 — 직접 생성 중..."
    multipass exec cloud -- sudo bash -c "
      TOKEN_VAL=\$(openssl rand -hex 32)
      kubectl create secret generic tokensecret -n kubeedge --from-literal=tokendata=\$TOKEN_VAL 2>/dev/null || true
    "
    sleep 5
    continue
  fi
  echo "  토큰 재시도 ($i/20)..."
  sleep 15
done
if [ -z "$TOKEN" ]; then
  red "토큰 획득 실패. CloudCore가 정상 기동되지 않았습니다."
  exit 1
fi
green "토큰 획득 완료"

#----------------------------------------------------------
# 4) Edge 노드 설정
#----------------------------------------------------------
step 4 "Edge 노드 2대 설정 (containerd + EdgeCore) — 약 2분"
for node in edge1 edge2; do
  echo "  $node 설정 중..."
  multipass exec $node -- bash -c "
    if pgrep edgecore &>/dev/null; then echo 'edgecore already running'; exit 0; fi
    sudo apt-get update -qq && sudo apt-get install -y -qq containerd >/dev/null
    sudo mkdir -p /etc/containerd && sudo containerd config default | sudo tee /etc/containerd/config.toml >/dev/null
    sudo sed -i 's/SystemdCgroup = true/SystemdCgroup = false/' /etc/containerd/config.toml
    sudo systemctl restart containerd
    sudo mkdir -p /opt/cni/bin
    curl -sL https://github.com/containernetworking/plugins/releases/download/v1.4.1/cni-plugins-linux-amd64-v1.4.1.tgz | sudo tar xz -C /opt/cni/bin/
    curl -sLO https://github.com/kubeedge/kubeedge/releases/download/v${KUBEEDGE_VER}/keadm-v${KUBEEDGE_VER}-linux-amd64.tar.gz
    tar xzf keadm-v${KUBEEDGE_VER}-linux-amd64.tar.gz
    sudo cp keadm-v${KUBEEDGE_VER}-linux-amd64/keadm/keadm /usr/local/bin/
    sudo keadm join --cloudcore-ipport=$CLOUD_IP:10000 --token=$TOKEN --kubeedge-version=${KUBEEDGE_VER} --remote-runtime-endpoint=unix:///run/containerd/containerd.sock 2>&1 || true
  "
done
green "Edge 노드 설정 완료"

#----------------------------------------------------------
# 5) Edge 노드 패치
#----------------------------------------------------------
step 5 "Edge 노드 패치 (metaServer, flannel, masquerade, edge 라벨)"
echo "  Edge 노드 등록 대기 중..."
multipass exec cloud -- bash -c "
  for i in \$(seq 1 30); do
    COUNT=\$(kubectl get nodes 2>/dev/null | grep -c edge)
    [ \$COUNT -ge 2 ] && break
    sleep 5
  done
  # k8s/agent-edge1.yaml, k8s/agent-edge2.yaml, k8s/gateway.yaml의
  # nodeSelector(node-role.kubernetes.io/edge)가 매칭되도록 edge 노드에 라벨을 붙인다.
  kubectl label node edge1 node-role.kubernetes.io/edge='' --overwrite 2>/dev/null || true
  kubectl label node edge2 node-role.kubernetes.io/edge='' --overwrite 2>/dev/null || true
"
for node in edge1 edge2; do
  multipass exec $node -- bash -c "
    sudo sed -i '/metaServer:/,+5{/enable: false/s/false/true/}' /etc/kubeedge/config/edgecore.yaml
    sudo sed -i '/edgeStream:/,+1{s/enable: false/enable: true/}' /etc/kubeedge/config/edgecore.yaml
    sudo systemctl restart edgecore
  "
done

EDGE1_CIDR=$(multipass exec cloud -- kubectl get node edge1 -o jsonpath='{.spec.podCIDR}')
EDGE2_CIDR=$(multipass exec cloud -- kubectl get node edge2 -o jsonpath='{.spec.podCIDR}')
multipass exec edge1 -- bash -c "sudo mkdir -p /run/flannel; echo 'FLANNEL_NETWORK=10.244.0.0/16
FLANNEL_SUBNET=${EDGE1_CIDR%.*}.1/24
FLANNEL_MTU=1450
FLANNEL_IPMASQ=true' | sudo tee /run/flannel/subnet.env >/dev/null"
multipass exec edge2 -- bash -c "sudo mkdir -p /run/flannel; echo 'FLANNEL_NETWORK=10.244.0.0/16
FLANNEL_SUBNET=${EDGE2_CIDR%.*}.1/24
FLANNEL_MTU=1450
FLANNEL_IPMASQ=true' | sudo tee /run/flannel/subnet.env >/dev/null"

multipass exec edge1 -- sudo iptables -t nat -A POSTROUTING -s ${EDGE1_CIDR} -o eth0 -j MASQUERADE 2>/dev/null || true
multipass exec edge2 -- sudo iptables -t nat -A POSTROUTING -s ${EDGE2_CIDR} -o eth0 -j MASQUERADE 2>/dev/null || true
green "Edge 패치 완료"

#----------------------------------------------------------
# 6) 이미지 빌드 (edge1에서 빌드 → cloud/edge2 전송) - 본 repo 소스 사용
#----------------------------------------------------------
step 6 "eam 이미지 빌드 (edge1에서 빌드, 본 repo 소스 사용) — 약 3분"
tar czf /tmp/eam-src.tar.gz -C "$REPO_ROOT" \
  --exclude='.git' --exclude='__pycache__' --exclude='.pytest_cache' \
  --exclude='prototype-y1' --exclude='.superpowers' --exclude='*.egg-info' \
  pyproject.toml README.md LICENSE src deploy
EAM_TAR_WIN="$(cygpath -w /tmp/eam-src.tar.gz 2>/dev/null || echo /tmp/eam-src.tar.gz)"
HOST_TMP="$(cygpath -w /tmp 2>/dev/null || echo /tmp)"

multipass transfer "$EAM_TAR_WIN" edge1:/tmp/eam-src.tar.gz
multipass exec edge1 -- bash -c "mkdir -p /tmp/eam && cd /tmp/eam && tar xzf /tmp/eam-src.tar.gz"
echo "  edge1 빌드 도구 설치 중..."
multipass exec edge1 -- bash -c "
  if command -v nerdctl &>/dev/null; then exit 0; fi
  curl -sL https://github.com/containerd/nerdctl/releases/download/v1.7.7/nerdctl-1.7.7-linux-amd64.tar.gz | sudo tar xz -C /usr/local/bin/
  curl -sL https://github.com/moby/buildkit/releases/download/v0.13.2/buildkit-v0.13.2.linux-amd64.tar.gz | sudo tar xz -C /usr/local/
  sudo buildkitd &>/dev/null &
  sleep 3
"

echo "  edge1 이미지 빌드 중 (${MANAGER_IMAGE}, ${AGENT_IMAGE})..."
multipass exec edge1 -- sudo bash -c "
  cd /tmp/eam
  nerdctl build -t ${MANAGER_IMAGE} -f deploy/Dockerfile.manager . >/dev/null 2>&1
  nerdctl build -t ${AGENT_IMAGE} -f deploy/Dockerfile.agent . >/dev/null 2>&1
  nerdctl save -o /tmp/mgr.tar ${MANAGER_IMAGE}
  nerdctl save -o /tmp/agent.tar ${AGENT_IMAGE}
  pkill -9 buildkitd 2>/dev/null || true
  nerdctl system prune -af >/dev/null 2>&1 || true
  echo 'edge1 build done'
"

echo "  cloud 로 이미지 전송 중..."
for img in mgr agent; do
  multipass transfer edge1:/tmp/${img}.tar "${HOST_TMP}\\${img}.tar"
  multipass transfer "${HOST_TMP}\\${img}.tar" cloud:/tmp/${img}.tar
  multipass exec cloud -- sudo k3s ctr images import /tmp/${img}.tar
  multipass exec cloud -- sudo rm -f /tmp/${img}.tar
  rm -f "${HOST_TMP}\\${img}.tar" 2>/dev/null || true
done

echo "  edge2 로 agent 이미지 전송 중..."
multipass transfer edge1:/tmp/agent.tar "${HOST_TMP}\\agent2.tar"
multipass transfer "${HOST_TMP}\\agent2.tar" edge2:/tmp/agent.tar
rm -f "${HOST_TMP}\\agent2.tar" 2>/dev/null || true
multipass exec edge2 -- sudo ctr -n k8s.io images import /tmp/agent.tar
multipass exec edge2 -- sudo rm -f /tmp/agent.tar

multipass exec edge1 -- sudo rm -f /tmp/mgr.tar /tmp/agent.tar
green "이미지 빌드 완료 (${MANAGER_IMAGE}, ${AGENT_IMAGE})"

#----------------------------------------------------------
# 7) Secret 생성 (부트스트랩 토큰/관리자·운영자 자격증명/RabbitMQ)
#----------------------------------------------------------
step 7 "Secret 생성 (bootstrap 토큰, 관리자/운영자 자격증명, RabbitMQ 자격증명·TLS)"
BOOTSTRAP_TOKEN=$(openssl rand -hex 32)
ADMIN_USERNAME="admin"
ADMIN_PASSWORD=$(openssl rand -base64 24)
OPERATOR_USERNAME="operator"
OPERATOR_PASSWORD=$(openssl rand -base64 24)
RABBITMQ_USERNAME="isl"
RABBITMQ_PASSWORD=$(openssl rand -base64 24)

multipass exec cloud -- bash -c "
  for attempt in 1 2 3; do
    kubectl create namespace edge-auth --dry-run=client -o yaml | kubectl apply -f - 2>/dev/null && break
    echo '  API 재시도 대기...'
    sleep 15
  done

  kubectl create secret generic eam-secrets -n edge-auth \
    --from-literal=bootstrap-token='$BOOTSTRAP_TOKEN' \
    --from-literal=admin-username='$ADMIN_USERNAME' \
    --from-literal=admin-password='$ADMIN_PASSWORD' \
    --from-literal=operator-username='$OPERATOR_USERNAME' \
    --from-literal=operator-password='$OPERATOR_PASSWORD' \
    --dry-run=client -o yaml | kubectl apply -f -

  kubectl create secret generic rabbitmq-secrets -n edge-auth \
    --from-literal=username='$RABBITMQ_USERNAME' \
    --from-literal=password='$RABBITMQ_PASSWORD' \
    --dry-run=client -o yaml | kubectl apply -f -

  # RabbitMQ 전용 자기서명 CA/서버 인증서 (eam 자체 PKI와는 무관한 데모용
  # TLS 인증서 - RabbitMQ는 선택 사항 데이터플레인이므로 여기서만 쓰인다).
  mkdir -p /tmp/rmq-certs && cd /tmp/rmq-certs
  openssl req -x509 -newkey rsa:2048 -nodes -keyout ca.key -out ca.crt -days 365 -subj '/CN=eam-demo-rabbitmq-ca' 2>/dev/null
  openssl req -newkey rsa:2048 -nodes -keyout server.key -out server.csr -subj '/CN=rabbitmq' 2>/dev/null
  openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial -out server.crt -days 365 2>/dev/null
  kubectl create secret generic rabbitmq-certs -n edge-auth \
    --from-file=ca.crt=ca.crt --from-file=server.crt=server.crt --from-file=server.key=server.key \
    --dry-run=client -o yaml | kubectl apply -f -
  rm -rf /tmp/rmq-certs
"
green "Secret 생성 완료"

#----------------------------------------------------------
# 8) K8s 리소스 배포
#----------------------------------------------------------
step 8 "Kubernetes 리소스 배포 (Manager, Agent x2, Gateway; RabbitMQ는 선택)"
multipass exec cloud -- bash -c "
  for i in \$(seq 1 30); do kubectl get nodes &>/dev/null && break; sleep 5; done
"

mkdir -p /tmp/eam-k8s
for f in namespace.yaml manager.yaml agent-edge1.yaml agent-edge2.yaml gateway.yaml rabbitmq.yaml; do
  sed "s/__CLOUD_IP__/$CLOUD_IP/g" "$REPO_ROOT/k8s/$f" > "/tmp/eam-k8s/$f"
  multipass transfer "$(cygpath -w /tmp/eam-k8s/$f 2>/dev/null || echo /tmp/eam-k8s/$f)" cloud:/tmp/$f
done

multipass exec cloud -- bash -c "
  kubectl apply -f /tmp/namespace.yaml
  kubectl apply -f /tmp/manager.yaml
  kubectl apply -f /tmp/agent-edge1.yaml
  kubectl apply -f /tmp/agent-edge2.yaml
  kubectl apply -f /tmp/gateway.yaml
  # RabbitMQ는 선택 사항 데이터플레인이다 - 필요할 때만 아래 줄의 주석을 풀거나
  # 'kubectl apply -f /tmp/rabbitmq.yaml'을 수동으로 실행한다.
  # kubectl apply -f /tmp/rabbitmq.yaml
"
green "K8s 리소스 배포 완료 (RabbitMQ 제외 - 선택 사항)"

#----------------------------------------------------------
# 9) 안정화 대기
#----------------------------------------------------------
step 9 "Pod 시작 대기 (약 90초)"
sleep 90
multipass exec cloud -- bash -c "
  for i in \$(seq 1 30); do kubectl get nodes &>/dev/null && break; sleep 5; done
"
# CrashLoopBackOff Pod 개별 재시작
CRASH_DEPLOYS=$(multipass exec cloud -- bash -c "kubectl get pods -n edge-auth 2>/dev/null | grep CrashLoopBackOff | awk '{print \\\$1}' | sed 's/-[a-z0-9]*-[a-z0-9]*$//' | sort -u" || true)
if [ -n "$CRASH_DEPLOYS" ]; then
  echo "  CrashLoopBackOff Pod 감지 — 개별 재시작 중..."
  for dep in $CRASH_DEPLOYS; do
    multipass exec cloud -- kubectl rollout restart deployment/$dep -n edge-auth 2>/dev/null || true
  done
  sleep 30
fi
multipass exec cloud -- kubectl get pods -n edge-auth -o wide 2>/dev/null || echo "  (Pod 상태 확인은 잠시 후 가능)"
green "배포 완료"

#----------------------------------------------------------
# 10) 결과 출력
#----------------------------------------------------------
step 10 "접속 정보"
echo ""
echo "=============================================="
echo "   KubeEdge 데모 환경이 준비되었습니다!"
echo "=============================================="
echo ""
echo "  Manager API:  http://$CLOUD_IP:30443/api/v1/healthz"
echo "  관리자 계정:   $ADMIN_USERNAME / $ADMIN_PASSWORD"
echo "  운영자 계정:   $OPERATOR_USERNAME / $OPERATOR_PASSWORD"
echo "  Bootstrap 토큰: $BOOTSTRAP_TOKEN"
echo "  (RabbitMQ는 선택 사항 - 배포하려면"
echo "   multipass exec cloud -- kubectl apply -f /tmp/rabbitmq.yaml"
echo "   ID: $RABBITMQ_USERNAME / PW: $RABBITMQ_PASSWORD)"
echo ""
echo "  노드 현황:"
multipass exec cloud -- kubectl get nodes
echo ""
echo "  보안 적용 전/후 비교 시연 (K8s 환경):"
echo "     kubectl set env deployment/manager -n edge-auth INSECURE_MODE=true"
echo "     kubectl rollout restart deployment/manager -n edge-auth   # '전' 상태"
echo "     kubectl set env deployment/manager -n edge-auth INSECURE_MODE=false"
echo "     kubectl rollout restart deployment/manager -n edge-auth   # '후' 상태"
echo "     (자세한 절차: demo/DEMO_SCENARIO.md)"
echo ""
echo "  환경 종료:"
echo "     bash demo-stop-v2.sh"
echo "=============================================="
