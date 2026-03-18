#!/bin/bash
###############################################################
#  KubeEdge + Edge-Auth-Manager 데모 환경 원클릭 구축 스크립트
#  - Windows (Git Bash) 에서 실행
#  - 필요: Multipass, Hyper-V 활성화
#  - K3s 기반 (kubeadm 대비 가볍고 안정적)
###############################################################
set -e
export MSYS_NO_PATHCONV=1

EAM_DIR="../edge-auth-manager-prototype-ISL"
CLOUD_IP=""
KUBEEDGE_VER="1.19.0"
K3S_VER="v1.29.15+k3s1"

red()   { echo -e "\033[31m$*\033[0m"; }
green() { echo -e "\033[32m$*\033[0m"; }
blue()  { echo -e "\033[34m$*\033[0m"; }
step()  { echo ""; blue "[$1/$TOTAL_STEPS] $2"; }

TOTAL_STEPS=10

#----------------------------------------------------------
# 사전 체크
#----------------------------------------------------------
step 0 "사전 요구사항 확인"
if ! command -v multipass &>/dev/null; then
  red "❌ Multipass가 설치되어 있지 않습니다."
  exit 1
fi
green "✅ Multipass 확인됨"

if [ ! -d "$EAM_DIR/certs" ]; then
  red "❌ edge-auth-manager-prototype-ISL 프로젝트를 찾을 수 없습니다."
  exit 1
fi
green "✅ edge-auth-manager 프로젝트 확인됨"

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
green "✅ cloud=$CLOUD_IP, edge1=$EDGE1_IP, edge2=$EDGE2_IP"

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
green "✅ Cloud 노드 설정 완료"

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
  red "❌ 토큰 획득 실패. CloudCore가 정상 기동되지 않았습니다."
  exit 1
fi
green "✅ 토큰 획득 완료"

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
green "✅ Edge 노드 설정 완료"

#----------------------------------------------------------
# 5) Edge 노드 패치
#----------------------------------------------------------
step 5 "Edge 노드 패치 (metaServer, flannel, masquerade)"
echo "  Edge 노드 등록 대기 중..."
multipass exec cloud -- bash -c "
  for i in \$(seq 1 30); do
    COUNT=\$(kubectl get nodes 2>/dev/null | grep -c edge)
    [ \$COUNT -ge 2 ] && break
    sleep 5
  done
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
# K3s는 kube-proxy DaemonSet이 없으므로 edge exclude 패치 불필요
green "✅ Edge 패치 완료"

#----------------------------------------------------------
# 6) 이미지 빌드 (edge1에서 빌드 → cloud/edge2 전송)
#----------------------------------------------------------
step 6 "edge-auth-manager 이미지 빌드 (edge1에서 빌드) — 약 5분"
cd "$(dirname "$0")"
tar czf /tmp/eam.tar.gz -C "$EAM_DIR" --exclude='data' --exclude='.git' --exclude='__pycache__' --exclude='.idea' .
EAM_TAR_WIN="$(cygpath -w /tmp/eam.tar.gz 2>/dev/null || echo /tmp/eam.tar.gz)"
HOST_TMP="$(cygpath -w /tmp 2>/dev/null || echo /tmp)"

multipass transfer "$EAM_TAR_WIN" edge1:/tmp/eam.tar.gz
multipass exec edge1 -- bash -c "mkdir -p /tmp/eam && cd /tmp/eam && tar xzf /tmp/eam.tar.gz"
echo "  edge1 빌드 도구 설치 중..."
multipass exec edge1 -- bash -c "
  if command -v nerdctl &>/dev/null; then exit 0; fi
  curl -sL https://github.com/containerd/nerdctl/releases/download/v1.7.7/nerdctl-1.7.7-linux-amd64.tar.gz | sudo tar xz -C /usr/local/bin/
  curl -sL https://github.com/moby/buildkit/releases/download/v0.13.2/buildkit-v0.13.2.linux-amd64.tar.gz | sudo tar xz -C /usr/local/
  sudo buildkitd &>/dev/null &
  sleep 3
"

echo "  edge1 전체 이미지 빌드 중..."
multipass exec edge1 -- sudo bash -c "
  cd /tmp/eam
  nerdctl build -t eam-manager:latest -f services/manager/Dockerfile . >/dev/null 2>&1
  nerdctl build -t eam-dashboard:latest -f services/dashboard/Dockerfile . >/dev/null 2>&1
  nerdctl build -t eam-agent:latest -f services/agent/Dockerfile . >/dev/null 2>&1
  nerdctl pull rabbitmq:3.13-management >/dev/null 2>&1
  nerdctl save -o /tmp/mgr.tar eam-manager:latest
  nerdctl save -o /tmp/dash.tar eam-dashboard:latest
  nerdctl save -o /tmp/agent.tar eam-agent:latest
  nerdctl save -o /tmp/rmq.tar rabbitmq:3.13-management
  ctr -n k8s.io images import /tmp/agent.tar
  pkill -9 buildkitd 2>/dev/null || true
  nerdctl system prune -af >/dev/null 2>&1 || true
  echo 'edge1 build done'
"

echo "  cloud 로 이미지 전송 중..."
for img in mgr dash rmq agent; do
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

multipass exec edge1 -- sudo rm -f /tmp/mgr.tar /tmp/dash.tar /tmp/rmq.tar /tmp/agent.tar

# cloud에 소스 전송 (인증서용)
multipass transfer "$EAM_TAR_WIN" cloud:/tmp/eam.tar.gz
multipass exec cloud -- bash -c "mkdir -p /tmp/eam && cd /tmp/eam && tar xzf /tmp/eam.tar.gz"
green "✅ 이미지 빌드 완료"

#----------------------------------------------------------
# 7) Manager 인증서 재생성
#----------------------------------------------------------
step 7 "Manager TLS 인증서 갱신 (Cloud IP SAN 추가)"
multipass exec cloud -- bash -c "
  cd /tmp/eam/certs
  openssl req -new -key manager/server.key -out /tmp/mgr.csr -subj '/CN=manager' \
    -addext 'subjectAltName=DNS:manager.local,DNS:manager,DNS:localhost,IP:$CLOUD_IP'
  openssl x509 -req -in /tmp/mgr.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
    -out /tmp/mgr.crt -days 365 \
    -extfile <(echo 'subjectAltName=DNS:manager.local,DNS:manager,DNS:localhost,IP:$CLOUD_IP') 2>/dev/null
  cp /tmp/mgr.crt manager/server.crt
"
green "✅ 인증서 갱신 완료"

#----------------------------------------------------------
# 8) K8s 리소스 배포
#----------------------------------------------------------
step 8 "Kubernetes 리소스 배포 (RabbitMQ, Manager, Dashboard, Agent x2)"
multipass exec cloud -- bash -c "
  for i in \$(seq 1 30); do kubectl get nodes &>/dev/null && break; sleep 5; done
"

SCRIPT_ROOT="$(dirname "$0")"
mkdir -p /tmp/eam-k8s
for f in namespace.yaml rabbitmq.yaml manager.yaml dashboard.yaml agent-edge1.yaml agent-edge2.yaml; do
  sed "s/172\.18\.78\.12/$CLOUD_IP/g" "$SCRIPT_ROOT/k8s/$f" > "/tmp/eam-k8s/$f"
  multipass transfer "$(cygpath -w /tmp/eam-k8s/$f 2>/dev/null || echo /tmp/eam-k8s/$f)" cloud:/tmp/$f
done

multipass exec cloud -- bash -c "
  for attempt in 1 2 3; do
    kubectl apply -f /tmp/namespace.yaml 2>/dev/null && break
    echo '  API 재시도 대기...'
    sleep 15
  done

  cd /tmp/eam/certs
  kubectl create secret generic ca-cert -n edge-auth --from-file=ca.crt=ca.crt 2>/dev/null || true
  kubectl create secret generic rabbitmq-certs -n edge-auth --from-file=ca.crt=rabbitmq/ca.crt --from-file=server.crt=rabbitmq/server.crt --from-file=server.key=rabbitmq/server.key 2>/dev/null || true
  kubectl create secret generic manager-certs -n edge-auth --from-file=ca.crt=manager/ca.crt --from-file=server.crt=manager/server.crt --from-file=server.key=manager/server.key 2>/dev/null || true
  kubectl create secret generic agent-certs -n edge-auth --from-file=ca.crt=agent/ca.crt --from-file=client.crt=agent/client.crt --from-file=client.key=agent/client.key 2>/dev/null || true
  kubectl create secret generic admin-certs -n edge-auth --from-file=ca.crt=admin/ca.crt --from-file=admin.crt=admin/client.crt --from-file=admin.key=admin/client.key 2>/dev/null || true

  kubectl apply -f /tmp/rabbitmq.yaml
  kubectl apply -f /tmp/manager.yaml
  kubectl apply -f /tmp/dashboard.yaml
  kubectl apply -f /tmp/agent-edge1.yaml
  kubectl apply -f /tmp/agent-edge2.yaml
"
green "✅ K8s 리소스 배포 완료"

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
green "✅ 배포 완료"

#----------------------------------------------------------
# 10) 결과 출력
#----------------------------------------------------------
step 10 "접속 정보"
echo ""
echo "=============================================="
echo "   KubeEdge 데모 환경이 준비되었습니다!"
echo "=============================================="
echo ""
echo "  📊 Dashboard:  http://$CLOUD_IP:30501"
echo "  🐰 RabbitMQ:   http://$CLOUD_IP:15672"
echo "     (ID: isl / PW: wjdqhqhghdusrntlf1!)"
echo ""
echo "  📡 노드 현황:"
multipass exec cloud -- kubectl get nodes
echo ""
echo "  🔧 통신 테스트 실행:"
echo "     bash demo-test.sh"
echo ""
echo "  🛑 환경 종료:"
echo "     bash demo-stop.sh"
echo "=============================================="
