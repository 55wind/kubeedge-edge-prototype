#!/bin/bash
###############################################################
#  KubeEdge + Edge-Auth-Manager 데모 환경 원클릭 구축 스크립트
#  - Windows (Git Bash) 에서 실행
#  - 필요: Multipass, Hyper-V 활성화
###############################################################
set -e
export MSYS_NO_PATHCONV=1

EAM_DIR="../edge-auth-manager-prototype-ISL"
CLOUD_IP=""
KUBEEDGE_VER="1.19.0"

red()   { echo -e "\033[31m$*\033[0m"; }
green() { echo -e "\033[32m$*\033[0m"; }
blue()  { echo -e "\033[34m$*\033[0m"; }
step()  { echo ""; blue "[$1/$TOTAL_STEPS] $2"; }

TOTAL_STEPS=11

#----------------------------------------------------------
# 사전 체크
#----------------------------------------------------------
step 0 "사전 요구사항 확인"
if ! command -v multipass &>/dev/null; then
  red "❌ Multipass가 설치되어 있지 않습니다."
  red "   https://multipass.run/install 에서 설치 후 다시 실행하세요."
  exit 1
fi
green "✅ Multipass 확인됨"

if [ ! -d "$EAM_DIR/certs" ]; then
  red "❌ edge-auth-manager-prototype-ISL 프로젝트를 찾을 수 없습니다."
  red "   이 스크립트와 같은 부모 폴더에 프로젝트가 있어야 합니다."
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
      multipass launch 22.04 -n $vm -c 4 -m 4G -d 20G
    else
      multipass launch 22.04 -n $vm -c 2 -m 1G -d 10G
    fi
  fi
done
CLOUD_IP=$(multipass info cloud --format csv | tail -1 | cut -d, -f3)
EDGE1_IP=$(multipass info edge1 --format csv | tail -1 | cut -d, -f3)
EDGE2_IP=$(multipass info edge2 --format csv | tail -1 | cut -d, -f3)
green "✅ cloud=$CLOUD_IP, edge1=$EDGE1_IP, edge2=$EDGE2_IP"

#----------------------------------------------------------
# 2) Cloud 노드: kubeadm + flannel + CloudCore
#----------------------------------------------------------
step 2 "Cloud 노드 설정 (Kubernetes + KubeEdge CloudCore) — 약 3분"
multipass exec cloud -- bash -c "
  if command -v kubectl &>/dev/null; then echo 'K8s already installed'; exit 0; fi
  # 커널 파라미터 설정 (kubeadm preflight 필수)
  sudo modprobe br_netfilter
  echo br_netfilter | sudo tee /etc/modules-load.d/br_netfilter.conf >/dev/null
  echo 'net.bridge.bridge-nf-call-iptables = 1
net.ipv4.ip_forward = 1' | sudo tee /etc/sysctl.d/k8s.conf >/dev/null
  sudo sysctl --system >/dev/null 2>&1
  sudo apt-get update -qq && sudo apt-get install -y -qq containerd apt-transport-https curl >/dev/null
  sudo mkdir -p /etc/containerd && sudo containerd config default | sudo tee /etc/containerd/config.toml >/dev/null
  sudo systemctl restart containerd
  curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.29/deb/Release.key | sudo gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg
  echo 'deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v1.29/deb/ /' | sudo tee /etc/apt/sources.list.d/kubernetes.list >/dev/null
  sudo apt-get update -qq && sudo apt-get install -y -qq kubelet kubeadm kubectl >/dev/null
  sudo kubeadm init --pod-network-cidr=10.244.0.0/16 --apiserver-advertise-address=$CLOUD_IP >/dev/null 2>&1
  mkdir -p ~/.kube && sudo cp /etc/kubernetes/admin.conf ~/.kube/config && sudo chown \$(id -u):\$(id -g) ~/.kube/config
  sudo mkdir -p /root/.kube && sudo cp /etc/kubernetes/admin.conf /root/.kube/config
  echo 'K8s init done'
"

echo "  API 서버 준비 대기 중..."
multipass exec cloud -- bash -c "
  for i in \$(seq 1 30); do kubectl get nodes &>/dev/null && break; echo '  대기 중...'; sleep 5; done
  kubectl taint nodes cloud node-role.kubernetes.io/control-plane:NoSchedule- 2>/dev/null || true
  kubectl apply -f https://github.com/flannel-io/flannel/releases/latest/download/kube-flannel.yml >/dev/null 2>&1
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
echo "  CloudCore Pod 준비 대기 중..."
sleep 30
multipass exec cloud -- bash -c "
  for i in \$(seq 1 60); do kubectl get pods -n kubeedge 2>/dev/null | grep -q '1/1.*Running' && break; sleep 5; done
"
TOKEN=""
for i in 1 2 3 4 5; do
  TOKEN=$(multipass exec cloud -- sudo keadm gettoken 2>/dev/null) && [ -n "$TOKEN" ] && break
  echo "  토큰 재시도 ($i/5)..."
  sleep 10
done
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
    # CNI plugins
    sudo mkdir -p /opt/cni/bin
    curl -sL https://github.com/containernetworking/plugins/releases/download/v1.4.1/cni-plugins-linux-amd64-v1.4.1.tgz | sudo tar xz -C /opt/cni/bin/
    # keadm join
    curl -sLO https://github.com/kubeedge/kubeedge/releases/download/v${KUBEEDGE_VER}/keadm-v${KUBEEDGE_VER}-linux-amd64.tar.gz
    tar xzf keadm-v${KUBEEDGE_VER}-linux-amd64.tar.gz
    sudo cp keadm-v${KUBEEDGE_VER}-linux-amd64/keadm/keadm /usr/local/bin/
    sudo keadm join --cloudcore-ipport=$CLOUD_IP:10000 --token=$TOKEN --kubeedge-version=${KUBEEDGE_VER} --remote-runtime-endpoint=unix:///run/containerd/containerd.sock
  "
done
green "✅ Edge 노드 설정 완료"

#----------------------------------------------------------
# 5) Edge 노드 패치 (metaServer, flannel subnet)
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
    # Enable metaServer
    sudo sed -i '/metaServer:/,+5{/enable: false/s/false/true/}' /etc/kubeedge/config/edgecore.yaml
    # Enable edgeStream
    sudo sed -i '/edgeStream:/,+1{s/enable: false/enable: true/}' /etc/kubeedge/config/edgecore.yaml
    sudo systemctl restart edgecore
  "
done

# flannel subnet.env
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

# masquerade
multipass exec edge1 -- sudo iptables -t nat -A POSTROUTING -s ${EDGE1_CIDR} -o eth0 -j MASQUERADE 2>/dev/null || true
multipass exec edge2 -- sudo iptables -t nat -A POSTROUTING -s ${EDGE2_CIDR} -o eth0 -j MASQUERADE 2>/dev/null || true

# kube-proxy exclude edge
multipass exec cloud -- kubectl patch daemonset kube-proxy -n kube-system --type merge \
  -p '{"spec":{"template":{"spec":{"affinity":{"nodeAffinity":{"requiredDuringSchedulingIgnoredDuringExecution":{"nodeSelectorTerms":[{"matchExpressions":[{"key":"node-role.kubernetes.io/edge","operator":"DoesNotExist"}]}]}}}}}}}' 2>/dev/null || true

green "✅ Edge 패치 완료"

#----------------------------------------------------------
# 6) 프로젝트 파일 전송 + 이미지 빌드
#----------------------------------------------------------
step 6 "edge-auth-manager 이미지 빌드 — 약 3분"
cd "$(dirname "$0")"
tar czf /tmp/eam.tar.gz -C "$EAM_DIR" --exclude='data' --exclude='.git' --exclude='__pycache__' --exclude='.idea' .
EAM_TAR_WIN="$(cygpath -w /tmp/eam.tar.gz 2>/dev/null || echo "$(cd /tmp && pwd -W 2>/dev/null || pwd)/eam.tar.gz")"

for vm in cloud edge1 edge2; do
  multipass transfer "$EAM_TAR_WIN" $vm:/tmp/eam.tar.gz
  multipass exec $vm -- bash -c "mkdir -p /tmp/eam && cd /tmp/eam && tar xzf /tmp/eam.tar.gz"
done

# nerdctl + buildkit 설치 및 빌드
for vm in cloud edge1 edge2; do
  echo "  $vm 에서 이미지 빌드 중..."
  multipass exec $vm -- bash -c "
    curl -sL https://github.com/containerd/nerdctl/releases/download/v1.7.7/nerdctl-1.7.7-linux-amd64.tar.gz | sudo tar xz -C /usr/local/bin/
    curl -sL https://github.com/moby/buildkit/releases/download/v0.13.2/buildkit-v0.13.2.linux-amd64.tar.gz | sudo tar xz -C /usr/local/
    sudo buildkitd &>/dev/null &
    sleep 3
  "
done

# Cloud: manager, dashboard, rabbitmq (파일 저장 방식 — pipe 실패 방지)
multipass exec cloud -- sudo bash -c "
  cd /tmp/eam
  nerdctl build -t eam-manager:latest -f services/manager/Dockerfile . >/dev/null 2>&1
  nerdctl build -t eam-dashboard:latest -f services/dashboard/Dockerfile . >/dev/null 2>&1
  nerdctl pull rabbitmq:3.13-management >/dev/null 2>&1
  nerdctl save -o /tmp/mgr.tar eam-manager:latest && ctr -n k8s.io images import /tmp/mgr.tar && rm -f /tmp/mgr.tar
  nerdctl save -o /tmp/dash.tar eam-dashboard:latest && ctr -n k8s.io images import /tmp/dash.tar && rm -f /tmp/dash.tar
  nerdctl save -o /tmp/rmq.tar rabbitmq:3.13-management && ctr -n k8s.io images import /tmp/rmq.tar && rm -f /tmp/rmq.tar
  pkill -9 buildkitd 2>/dev/null || true
  nerdctl system prune -af >/dev/null 2>&1 || true
  echo 'cloud images done'
"

# Edge: agent (파일 저장 방식)
for node in edge1 edge2; do
  multipass exec $node -- sudo bash -c "
    cd /tmp/eam
    nerdctl build -t eam-agent:latest -f services/agent/Dockerfile . >/dev/null 2>&1
    nerdctl save -o /tmp/agent.tar eam-agent:latest && ctr -n k8s.io images import /tmp/agent.tar && rm -f /tmp/agent.tar
    pkill -9 buildkitd 2>/dev/null || true
    nerdctl system prune -af >/dev/null 2>&1 || true
    echo '$node agent image done'
  "
done
green "✅ 이미지 빌드 완료"

#----------------------------------------------------------
# 7) Manager 인증서 재생성 (Cloud IP 포함)
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
# 7.5) IP 자동 복구 서비스 설치
#----------------------------------------------------------
step 8 "IP 자동 복구 서비스 설치 (VM 재시작 시 IP 변경 자동 대응)"

# CA 인증서를 영구 위치에 복사 (VM 재시작 후에도 사용 가능)
multipass exec cloud -- sudo bash -c "
  mkdir -p /etc/eam-certs
  cp /tmp/eam/certs/ca.crt /etc/eam-certs/
  cp /tmp/eam/certs/ca.key /etc/eam-certs/
  cp /tmp/eam/certs/manager/server.key /etc/eam-certs/manager-server.key
  chmod 600 /etc/eam-certs/ca.key /etc/eam-certs/manager-server.key
"

# Cloud VM: k8s-ip-fixup.sh (kubelet 시작 전 실행)
multipass exec cloud -- sudo bash -c 'cat > /usr/local/bin/k8s-ip-fixup.sh << '\''SCRIPT'\''
#!/bin/bash
# K8s IP Fixup: VM 부팅 시 DHCP IP 변경을 감지하고 K8s 설정을 자동 업데이트
set -e
STATE_DIR=/etc/k8s-ip-fixup
STATE_FILE=$STATE_DIR/last-ip
mkdir -p $STATE_DIR

# 현재 IP 획득 (네트워크 준비까지 대기)
for i in $(seq 1 30); do
  NEW_IP=$(ip -4 addr show eth0 2>/dev/null | grep -oP "inet \K[\d.]+")
  [ -n "$NEW_IP" ] && break
  sleep 2
done
[ -z "$NEW_IP" ] && { echo "ERROR: cannot detect IP"; exit 1; }

OLD_IP=""
[ -f "$STATE_FILE" ] && OLD_IP=$(cat "$STATE_FILE")

if [ "$NEW_IP" = "$OLD_IP" ]; then
  echo "IP unchanged ($NEW_IP), skipping fixup"
  exit 0
fi
echo "IP changed: $OLD_IP -> $NEW_IP"

if [ -n "$OLD_IP" ]; then
  # K8s static pod manifests
  for f in /etc/kubernetes/manifests/etcd.yaml /etc/kubernetes/manifests/kube-apiserver.yaml; do
    [ -f "$f" ] && sed -i "s/$OLD_IP/$NEW_IP/g" "$f"
  done

  # Kubeconfig files
  for f in /etc/kubernetes/admin.conf /etc/kubernetes/kubelet.conf \
           /etc/kubernetes/scheduler.conf /etc/kubernetes/controller-manager.conf \
           /etc/kubernetes/super-admin.conf /root/.kube/config /home/ubuntu/.kube/config; do
    [ -f "$f" ] && sed -i "s/$OLD_IP/$NEW_IP/g" "$f"
  done

  # API server 인증서 재생성 (SAN에 새 IP 포함)
  rm -f /etc/kubernetes/pki/apiserver.crt /etc/kubernetes/pki/apiserver.key
  kubeadm init phase certs apiserver --apiserver-advertise-address=$NEW_IP 2>/dev/null || {
    # kubeadm 실패 시 openssl로 직접 생성
    openssl req -new -nodes -keyout /etc/kubernetes/pki/apiserver.key \
      -out /tmp/apiserver.csr -subj "/CN=kube-apiserver" \
      -addext "subjectAltName=DNS:kubernetes,DNS:kubernetes.default,DNS:kubernetes.default.svc,DNS:kubernetes.default.svc.cluster.local,DNS:cloud,IP:10.96.0.1,IP:$NEW_IP,IP:127.0.0.1" 2>/dev/null
    openssl x509 -req -in /tmp/apiserver.csr \
      -CA /etc/kubernetes/pki/ca.crt -CAkey /etc/kubernetes/pki/ca.key \
      -CAcreateserial -out /etc/kubernetes/pki/apiserver.crt -days 365 \
      -extfile <(echo "subjectAltName=DNS:kubernetes,DNS:kubernetes.default,DNS:kubernetes.default.svc,DNS:kubernetes.default.svc.cluster.local,DNS:cloud,IP:10.96.0.1,IP:$NEW_IP,IP:127.0.0.1") 2>/dev/null
  }
fi

echo "$NEW_IP" > "$STATE_FILE"
echo "IP fixup completed: $NEW_IP"
SCRIPT
chmod +x /usr/local/bin/k8s-ip-fixup.sh'

# Cloud VM: k8s-app-fixup.sh (kubelet 시작 후 실행 — 앱 매니페스트 업데이트)
multipass exec cloud -- sudo bash -c 'cat > /usr/local/bin/k8s-app-fixup.sh << '\''SCRIPT'\''
#!/bin/bash
# K8s App Fixup: API 서버 안정화 후 앱 배포의 IP를 업데이트
set -e
STATE_DIR=/etc/k8s-ip-fixup
STATE_FILE=$STATE_DIR/last-ip
OLD_APP_IP_FILE=$STATE_DIR/last-app-ip

NEW_IP=$(cat "$STATE_FILE" 2>/dev/null)
[ -z "$NEW_IP" ] && exit 0

OLD_APP_IP=""
[ -f "$OLD_APP_IP_FILE" ] && OLD_APP_IP=$(cat "$OLD_APP_IP_FILE")
[ "$NEW_IP" = "$OLD_APP_IP" ] && { echo "App IPs already up to date"; exit 0; }

# API 서버 준비 대기
export KUBECONFIG=/root/.kube/config
for i in $(seq 1 60); do
  kubectl get nodes &>/dev/null && break
  sleep 5
done
kubectl get nodes &>/dev/null || { echo "API server not ready, skipping app fixup"; exit 0; }

echo "Updating app deployments to IP: $NEW_IP"

# Manager TLS 인증서 재생성
if [ -f /etc/eam-certs/ca.key ] && [ -n "$OLD_APP_IP" ]; then
  openssl req -new -key /etc/eam-certs/manager-server.key -out /tmp/mgr.csr -subj "/CN=manager" \
    -addext "subjectAltName=DNS:manager.local,DNS:manager,DNS:localhost,IP:$NEW_IP" 2>/dev/null
  openssl x509 -req -in /tmp/mgr.csr -CA /etc/eam-certs/ca.crt -CAkey /etc/eam-certs/ca.key \
    -CAcreateserial -out /tmp/mgr.crt -days 365 \
    -extfile <(echo "subjectAltName=DNS:manager.local,DNS:manager,DNS:localhost,IP:$NEW_IP") 2>/dev/null
  # manager-certs secret 업데이트
  kubectl create secret generic manager-certs -n edge-auth \
    --from-file=ca.crt=/etc/eam-certs/ca.crt \
    --from-file=server.crt=/tmp/mgr.crt \
    --from-file=server.key=/etc/eam-certs/manager-server.key \
    --dry-run=client -o yaml | kubectl apply -f - 2>/dev/null || true
fi

# 앱 배포 환경변수 업데이트
AMQP_PW="wjdqhqhghdusrntlf1!"
kubectl set env deployment/agent-001 -n edge-auth \
  MANAGER_BASE_URL="https://$NEW_IP:8443" \
  AMQP_URL="amqps://isl:${AMQP_PW}@${NEW_IP}:5671/" 2>/dev/null || true
kubectl set env deployment/agent-002 -n edge-auth \
  MANAGER_BASE_URL="https://$NEW_IP:8443" \
  AMQP_URL="amqps://isl:${AMQP_PW}@${NEW_IP}:5671/" 2>/dev/null || true
kubectl set env deployment/dashboard -n edge-auth \
  MANAGER_BASE_URL="https://$NEW_IP:8443" \
  RABBITMQ_HOST="$NEW_IP" 2>/dev/null || true

echo "$NEW_IP" > "$OLD_APP_IP_FILE"
echo "App fixup completed: $NEW_IP"
SCRIPT
chmod +x /usr/local/bin/k8s-app-fixup.sh'

# Cloud VM: systemd 서비스 등록
multipass exec cloud -- sudo bash -c '
# kubelet 전에 실행: K8s 설정 + 인증서 업데이트
cat > /etc/systemd/system/k8s-ip-fixup.service << EOF
[Unit]
Description=Fix Kubernetes IPs after DHCP change
Before=kubelet.service
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/k8s-ip-fixup.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

# kubelet 후에 실행: 앱 매니페스트 업데이트
cat > /etc/systemd/system/k8s-app-fixup.service << EOF
[Unit]
Description=Fix K8s app deployments after IP change
After=kubelet.service
Wants=kubelet.service

[Service]
Type=oneshot
ExecStart=/usr/local/bin/k8s-app-fixup.sh
RemainAfterExit=yes
TimeoutStartSec=600

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable k8s-ip-fixup k8s-app-fixup
'

# 초기 IP 저장
multipass exec cloud -- sudo bash -c "mkdir -p /etc/k8s-ip-fixup; echo '$CLOUD_IP' > /etc/k8s-ip-fixup/last-ip; echo '$CLOUD_IP' > /etc/k8s-ip-fixup/last-app-ip"

# Edge VM: edgecore-ip-fixup.sh (cloud.mshome.net 호스트명으로 IP 자동 검색)
for node in edge1 edge2; do
  multipass exec $node -- sudo bash -c 'cat > /usr/local/bin/edgecore-ip-fixup.sh << '\''SCRIPT'\''
#!/bin/bash
# EdgeCore IP Fixup: cloud VM의 현재 IP를 DNS로 확인하고 edgecore 설정 업데이트
set -e
STATE_DIR=/etc/edgecore-ip-fixup
STATE_FILE=$STATE_DIR/last-cloud-ip
mkdir -p $STATE_DIR

# cloud.mshome.net DNS로 현재 cloud IP 확인 (최대 60초 대기)
CLOUD_IP=""
for i in $(seq 1 30); do
  CLOUD_IP=$(getent hosts cloud.mshome.net 2>/dev/null | awk "{print \$1}" | head -1)
  [ -n "$CLOUD_IP" ] && break
  CLOUD_IP=$(getent hosts cloud 2>/dev/null | awk "{print \$1}" | head -1)
  [ -n "$CLOUD_IP" ] && break
  sleep 2
done
[ -z "$CLOUD_IP" ] && { echo "ERROR: cannot resolve cloud IP"; exit 1; }

OLD_IP=""
[ -f "$STATE_FILE" ] && OLD_IP=$(cat "$STATE_FILE")

if [ "$CLOUD_IP" = "$OLD_IP" ]; then
  echo "Cloud IP unchanged ($CLOUD_IP), skipping"
  exit 0
fi
echo "Cloud IP changed: $OLD_IP -> $CLOUD_IP"

if [ -n "$OLD_IP" ] && [ -f /etc/kubeedge/config/edgecore.yaml ]; then
  sed -i "s/$OLD_IP/$CLOUD_IP/g" /etc/kubeedge/config/edgecore.yaml
fi

echo "$CLOUD_IP" > "$STATE_FILE"
echo "EdgeCore IP fixup completed: cloud=$CLOUD_IP"
SCRIPT
chmod +x /usr/local/bin/edgecore-ip-fixup.sh'

  multipass exec $node -- sudo bash -c '
cat > /etc/systemd/system/edgecore-ip-fixup.service << EOF
[Unit]
Description=Fix EdgeCore cloudcore IP after DHCP change
Before=edgecore.service
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/edgecore-ip-fixup.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable edgecore-ip-fixup
'
  multipass exec $node -- sudo bash -c "mkdir -p /etc/edgecore-ip-fixup; echo '$CLOUD_IP' > /etc/edgecore-ip-fixup/last-cloud-ip"
done
green "✅ IP 자동 복구 서비스 설치 완료"

#----------------------------------------------------------
# 9) K8s 리소스 배포
#----------------------------------------------------------
step 9 "Kubernetes 리소스 배포 (RabbitMQ, Manager, Dashboard, Agent x2)"
echo "  Control plane 안정화 중..."
# 이미지 빌드 후 etcd↔apiserver CrashLoopBackOff 악순환 해소
# kubelet restart로 backoff 카운터를 리셋하고, control plane이 완전히 안정될 때까지 대기
multipass exec cloud -- sudo systemctl restart kubelet
sleep 30
# etcd + apiserver가 모두 Running 상태가 될 때까지 대기 (최대 5분)
multipass exec cloud -- sudo bash -c "
  for i in \$(seq 1 60); do
    ETCD_OK=\$(crictl ps --name etcd 2>/dev/null | grep -c Running)
    API_OK=\$(crictl ps --name kube-apiserver 2>/dev/null | grep -c Running)
    [ \$ETCD_OK -ge 1 ] && [ \$API_OK -ge 1 ] && break
    sleep 5
  done
"
# kubectl이 연속 3회 성공해야 안정화로 판단 (일시적 응답 후 재크래시 방지)
multipass exec cloud -- bash -c "
  SUCCESS=0
  for i in \$(seq 1 60); do
    if kubectl get nodes &>/dev/null; then
      SUCCESS=\$((SUCCESS + 1))
      [ \$SUCCESS -ge 3 ] && break
    else
      SUCCESS=0
    fi
    sleep 5
  done
"
# 매니페스트 전송 (IP 치환 후 전송)
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

  # Secrets
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
# 10) 안정화 대기
#----------------------------------------------------------
step 10 "Pod 시작 대기 (약 90초)"
sleep 90
# API 서버 응답 확인
multipass exec cloud -- bash -c "
  for i in \$(seq 1 30); do kubectl get nodes &>/dev/null && break; sleep 5; done
"
# RabbitMQ 등 CrashLoopBackOff에 빠진 Pod가 있으면 kubelet restart로 즉시 복구
CRASH_COUNT=$(multipass exec cloud -- kubectl get pods -n edge-auth 2>/dev/null | grep -c CrashLoopBackOff || echo 0)
if [ "$CRASH_COUNT" -gt 0 ]; then
  echo "  CrashLoopBackOff Pod 감지 — kubelet 재시작으로 복구 중..."
  multipass exec cloud -- sudo systemctl restart kubelet
  sleep 30
  multipass exec cloud -- bash -c "
    for i in \$(seq 1 30); do kubectl get nodes &>/dev/null && break; sleep 5; done
  "
fi
multipass exec cloud -- kubectl get pods -n edge-auth -o wide 2>/dev/null || echo "  (Pod 상태 확인은 잠시 후 가능)"
green "✅ 배포 완료"

#----------------------------------------------------------
# 11) 결과 출력
#----------------------------------------------------------
step 11 "접속 정보"
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
