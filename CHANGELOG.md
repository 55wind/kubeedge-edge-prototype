# demo-setup.sh 변경 이력

## 변경 일자: 2026-03-16

### 변경 대상
- `demo-setup.sh` (1개 파일만 수정, 프로젝트 메인 소스코드 변경 없음)

### 변경 원인
- Canonical(Ubuntu)에서 Ubuntu 22.04 클라우드 이미지를 업데이트하면서 커널 기본값이 변경됨
- Multipass는 VM 생성 시 최신 이미지를 다운로드하므로, 이전에 동작하던 스크립트가 실패하게 됨

---

## 변경 1: Cloud VM 메모리 2G → 4G

### 위치
`demo-setup.sh` Step 1 — VM 생성 (line 49)

### 기존 코드
```bash
multipass launch 22.04 -n $vm -c 2 -m 2G -d 10G
```

### 변경 후 코드
```bash
multipass launch 22.04 -n $vm -c 2 -m 4G -d 10G
```

### 변경 이유
- Step 6에서 nerdctl build/pull로 이미지를 빌드할 때 메모리 사용량이 급증
- 2~3G에서는 kube-apiserver가 OOM으로 종료되어 이후 kubectl 명령이 실패함
- 4G로 증가시켜 이미지 빌드 후에도 API 서버가 안정적으로 유지되도록 변경

---

## 변경 2: 커널 파라미터 설정 추가

### 위치
`demo-setup.sh` Step 2 — `kubeadm init` 실행 전 (line 66~71)

### 기존 코드
```bash
if command -v kubectl &>/dev/null; then echo 'K8s already installed'; exit 0; fi
sudo apt-get update -qq && sudo apt-get install -y -qq containerd apt-transport-https curl >/dev/null
```

### 변경 후 코드
```bash
if command -v kubectl &>/dev/null; then echo 'K8s already installed'; exit 0; fi
# 커널 파라미터 설정 (kubeadm preflight 필수)
sudo modprobe br_netfilter
echo br_netfilter | sudo tee /etc/modules-load.d/br_netfilter.conf >/dev/null
echo 'net.bridge.bridge-nf-call-iptables = 1
net.ipv4.ip_forward = 1' | sudo tee /etc/sysctl.d/k8s.conf >/dev/null
sudo sysctl --system >/dev/null 2>&1
sudo apt-get update -qq && sudo apt-get install -y -qq containerd apt-transport-https curl >/dev/null
```

### 변경 이유
- 최신 Ubuntu 22.04 클라우드 이미지에서 `br_netfilter` 모듈과 `ip_forward`가 기본 비활성화됨
- `kubeadm init`의 preflight check에서 이 두 값을 필수로 요구하여 초기화가 실패함
- 에러 메시지: `[ERROR FileContent--proc-sys-net-bridge-bridge-nf-call-iptables]: /proc/sys/net/bridge/bridge-nf-call-iptables does not exist`

---

## 변경 3: /root/.kube 디렉토리 생성 추가

### 위치
`demo-setup.sh` Step 2 — kube config 복사 부분 (line 80)

### 기존 코드
```bash
sudo cp /etc/kubernetes/admin.conf /root/.kube/config 2>/dev/null || true
```

### 변경 후 코드
```bash
sudo mkdir -p /root/.kube && sudo cp /etc/kubernetes/admin.conf /root/.kube/config
```

### 변경 이유
- 최신 Ubuntu 이미지에서 `/root/.kube/` 디렉토리가 기본으로 존재하지 않음
- 디렉토리가 없어 복사가 실패했으나 `|| true`로 에러가 무시됨
- 이후 `keadm init`이 `/root/.kube/config`를 찾지 못해 CloudCore 설치 실패

---

## 변경 4: API 서버 대기 + taint 해제/flannel 순서 변경

### 위치
`demo-setup.sh` Step 2 — `kubeadm init` 완료 후, `keadm init` 실행 전 (line 84~89)

### 기존 코드
```bash
# kubeadm init 블록 내부에서 taint 해제 및 flannel 설치 실행
  kubectl taint nodes cloud node-role.kubernetes.io/control-plane:NoSchedule- 2>/dev/null || true
  kubectl apply -f https://github.com/flannel-io/flannel/releases/latest/download/kube-flannel.yml >/dev/null 2>&1

echo "  CloudCore 설치 중..."
```

### 변경 후 코드
```bash
echo "  API 서버 준비 대기 중..."
multipass exec cloud -- bash -c "
  for i in \$(seq 1 30); do kubectl get nodes &>/dev/null && break; echo '  대기 중...'; sleep 5; done
  kubectl taint nodes cloud node-role.kubernetes.io/control-plane:NoSchedule- 2>/dev/null || true
  kubectl apply -f https://github.com/flannel-io/flannel/releases/latest/download/kube-flannel.yml >/dev/null 2>&1
"
echo "  CloudCore 설치 중..."
```

### 변경 이유
- `kubeadm init` 직후 K8s API 서버가 완전히 준비되기 전에 taint 해제와 flannel 설치가 실행됨
- API 서버 미준비 상태에서 kubectl 명령이 실패하여 flannel 미설치, taint 해제 안됨
- 최대 150초(5초 × 30회)까지 API 서버 응답을 확인한 후 진행하도록 분리

---

## 변경 5: keadm init에 --force 플래그 추가

### 위치
`demo-setup.sh` Step 2 — CloudCore 설치 (line 96)

### 기존 코드
```bash
sudo keadm init --advertise-address=$CLOUD_IP --kubeedge-version=${KUBEEDGE_VER}
```

### 변경 후 코드
```bash
sudo keadm init --advertise-address=$CLOUD_IP --kubeedge-version=${KUBEEDGE_VER} --force
```

### 변경 이유
- `keadm init`은 기본적으로 CloudCore Pod가 Ready 상태가 될 때까지 대기함
- Multipass NAT 환경에서 GitHub/Docker Hub 네트워크가 느려 Helm 배포 타임아웃 발생
- `--force` 플래그로 Pod Ready 대기를 생략하고, Step 3에서 별도로 Pod 준비 확인

---

## 변경 6: CloudCore Pod 준비 대기 + 토큰 재시도 로직

### 위치
`demo-setup.sh` Step 3 — 토큰 획득 (line 104~114)

### 기존 코드
```bash
TOKEN=$(multipass exec cloud -- sudo keadm gettoken 2>/dev/null)
```

### 변경 후 코드
```bash
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
```

### 변경 이유
- 변경 5에서 `--force`로 Pod Ready 대기를 생략했으므로 별도 대기 필요
- CloudCore Pod가 Running 상태가 아니면 `keadm gettoken`이 빈 값을 반환
- 30초 초기 대기 + 최대 300초 Pod 준비 확인 + 5회 토큰 재시도로 안정성 확보

---

## 변경 7: Edge 노드 등록 대기 로직 추가

### 위치
`demo-setup.sh` Step 5 — Edge 패치 전 (line 145~152)

### 기존 코드
```bash
step 5 "Edge 노드 패치 (metaServer, flannel, masquerade)"
for node in edge1 edge2; do
```

### 변경 후 코드
```bash
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
```

### 변경 이유
- Step 4에서 `keadm join` 완료 후 edge 노드가 cloud에 등록되기까지 시간 소요
- 등록 전에 `kubectl get node edge1 -o jsonpath='{.spec.podCIDR}'` 실행 시 빈 값 반환
- flannel subnet.env가 잘못 생성되어 Pod 네트워킹 실패
- 최대 150초까지 2대 모두 등록 확인 후 진행

---

## 변경 8: 프로젝트 파일 전송 경로 수정

### 위치
`demo-setup.sh` Step 6 — 파일 전송 (line 190~191)

### 기존 코드
```bash
tar czf ~/eam.tar.gz -C "$EAM_DIR" --exclude='data' --exclude='.git' --exclude='__pycache__' --exclude='.idea' .
multipass transfer ~/eam.tar.gz $vm:/tmp/eam.tar.gz
```

### 변경 후 코드
```bash
tar czf /tmp/eam.tar.gz -C "$EAM_DIR" --exclude='data' --exclude='.git' --exclude='__pycache__' --exclude='.idea' .
EAM_TAR_WIN="$(cygpath -w /tmp/eam.tar.gz 2>/dev/null || echo "$(cd /tmp && pwd -W 2>/dev/null || pwd)/eam.tar.gz")"
multipass transfer "$EAM_TAR_WIN" $vm:/tmp/eam.tar.gz
```

### 변경 이유
- Git Bash에서 `~/eam.tar.gz`는 `/c/Users/사용자/eam.tar.gz`로 확장됨
- Multipass transfer는 Windows 네이티브 경로(`C:\Users\...`)를 요구
- `cygpath -w`로 Git Bash 경로를 Windows 경로로 변환하여 전송 성공 보장

---

## 변경 9: Step 8 API 서버 안정화 대기 + 재시도 로직

### 위치
`demo-setup.sh` Step 8 — K8s 리소스 배포 (line 251~266)

### 기존 코드
```bash
step 8 "Kubernetes 리소스 배포 (RabbitMQ, Manager, Dashboard, Agent x2)"
# 즉시 매니페스트 전송 및 apply 실행
multipass exec cloud -- bash -c "
  kubectl apply -f /tmp/namespace.yaml
```

### 변경 후 코드
```bash
step 8 "Kubernetes 리소스 배포 (RabbitMQ, Manager, Dashboard, Agent x2)"
echo "  API 서버 안정화 대기 중..."
sleep 30
multipass exec cloud -- bash -c "
  for i in \$(seq 1 60); do kubectl get nodes &>/dev/null && break; sleep 5; done
"
# 매니페스트 전송 후 apply
multipass exec cloud -- bash -c "
  for attempt in 1 2 3; do
    kubectl apply -f /tmp/namespace.yaml 2>/dev/null && break
    echo '  API 재시도 대기...'
    sleep 15
  done
```

### 변경 이유
- Step 6의 nerdctl build/pull 과정에서 메모리를 대량 소비하여 kube-apiserver가 일시적으로 OOM 종료됨
- kubelet이 static pod로 API 서버를 자동 재시작하지만, 복구까지 30~60초 소요
- 30초 초기 대기 + 최대 300초 API 확인 + namespace apply 3회 재시도로 안정성 확보

---

## 요약

| # | 변경 내용 | 추가/수정 | 원인 |
|---|----------|----------|------|
| 1 | Cloud VM 메모리 4G | 1줄 수정 | 이미지 빌드 시 API 서버 OOM |
| 2 | 커널 파라미터 설정 | 5줄 추가 | Ubuntu 이미지 기본값 변경 |
| 3 | /root/.kube 디렉토리 생성 | 1줄 수정 | 디렉토리 미존재로 config 복사 실패 |
| 4 | API 서버 대기 + taint/flannel 순서 변경 | 6줄 추가 | API 미준비 시 kubectl 실패 |
| 5 | keadm init --force | 1줄 수정 | Helm 배포 타임아웃 |
| 6 | CloudCore Pod 대기 + 토큰 재시도 | 10줄 추가 | --force로 인한 Pod 미준비 |
| 7 | Edge 노드 등록 대기 | 7줄 추가 | 미등록 상태에서 podCIDR 조회 실패 |
| 8 | 파일 전송 경로 수정 | 2줄 수정 | Git Bash ↔ Multipass 경로 비호환 |
| 9 | Step 8 API 안정화 대기 + 재시도 | 8줄 추가 | 이미지 빌드 후 API 서버 OOM 복구 대기 |
