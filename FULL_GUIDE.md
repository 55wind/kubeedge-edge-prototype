# KubeEdge 데모 환경 완벽 가이드

> 이 문서는 프로그래밍/서버/네트워크 지식이 전혀 없는 사람도 이해할 수 있도록 작성되었습니다.
> "이게 뭔데?", "왜 이런 문제가 생겼는데?", "어떻게 해결했는데?", "나는 뭘 하면 되는데?" 에 대한 답을 모두 담았습니다.

---

## 1. 이 프로젝트가 뭔가요?

### 한 줄 요약
**PC 한 대에서 "클라우드 서버 1대 + 엣지(현장) 장비 2대"를 가상으로 만들어서, 장비끼리 메시지를 주고받는 것을 시연하는 데모 환경입니다.**

### 비유로 설명
```
현실 세계:
  본사 서버실 ─── 인터넷 ──┬── 공장A의 센서 컴퓨터
                            └── 공장B의 센서 컴퓨터

이 데모:
  내 PC 안에 가상 컴퓨터 3대를 만들어서 위 구조를 흉내냄
```

- **Cloud 노드** = 본사 서버실 역할. 모든 걸 관리하는 중앙 컴퓨터
- **Edge1 노드** = 공장A에 설치된 센서 컴퓨터 (온도 측정)
- **Edge2 노드** = 공장B에 설치된 센서 컴퓨터 (습도 측정)

### 구조도
```
                    ┌─────────────────────┐
                    │     Cloud 노드       │
                    │    (가상 컴퓨터 1)     │
                    │                      │
                    │  Manager  = 인증 담당  │
                    │  RabbitMQ = 우체국     │
                    │  Dashboard = 화면     │
                    └──────────┬───────────┘
                               │
                    ┌──────────┴──────────┐
              ┌─────┴──────┐       ┌─────┴──────┐
              │  Edge1 노드  │       │  Edge2 노드  │
              │ (가상 컴퓨터 2) │       │ (가상 컴퓨터 3) │
              │              │       │              │
              │  Agent-001   │       │  Agent-002   │
              │  온도 센서     │       │  습도 센서     │
              └──────────────┘       └──────────────┘
```

### 사용된 기술 (몰라도 됩니다)

| 용어 | 쉬운 설명 |
|------|----------|
| **Multipass** | PC 안에 가상 컴퓨터(VM)를 만들어주는 프로그램 |
| **Kubernetes (K8s)** | 여러 컴퓨터에서 프로그램을 자동으로 배치하고 관리하는 시스템 |
| **KubeEdge** | Kubernetes를 인터넷이 불안정한 현장(공장, 농장 등)까지 확장하는 기술 |
| **RabbitMQ** | 프로그램끼리 메시지를 주고받게 해주는 우체국 같은 것 |
| **containerd** | 프로그램을 "컨테이너"라는 상자에 넣어서 실행하는 엔진 |
| **Pod** | Kubernetes에서 프로그램 하나를 실행하는 최소 단위 |
| **Node** | Kubernetes가 관리하는 컴퓨터 1대 |

---

## 2. 준비물

| 항목 | 설명 | 확인 방법 |
|------|------|----------|
| Windows 10/11 PC | RAM **8GB 이상** (16GB 권장) | 설정 → 시스템 → 정보 |
| Hyper-V | Windows 가상화 기능 | 아래 설치법 참고 |
| Multipass | VM 관리 프로그램 | `multipass version` 입력 |
| Git Bash | 리눅스 명령어를 쓸 수 있는 터미널 | 시작 메뉴에서 "Git Bash" 검색 |

### Hyper-V 켜는 법
1. 시작 메뉴에서 **PowerShell**을 검색하고 **관리자 권한으로 실행**
2. 아래 명령어 복사 → 붙여넣기 → Enter:
   ```
   Enable-WindowsOptionalFeature -Online -FeatureName Microsoft-Hyper-V -All
   ```
3. PC 재부팅

### Multipass 설치
1. https://multipass.run/install 에서 Windows 버전 다운로드
2. 설치 완료 후 Git Bash에서 확인:
   ```bash
   multipass version
   ```
   버전 번호가 나오면 성공

### Git Bash 설치
1. https://git-scm.com/download/win 에서 다운로드
2. 설치 (모든 옵션 기본값으로 OK)

---

## 3. 어떤 문제가 있었고, 어떻게 해결했나?

### 문제의 원인 (한 줄 요약)
**Ubuntu(운영체제)를 만드는 회사(Canonical)가 2026년 초에 보안 설정을 바꿨고, 그 때문에 원래 잘 되던 설치 스크립트가 안 되기 시작했습니다.**

### 상세 설명

이 데모는 `demo-setup.sh`라는 스크립트 파일 하나로 모든 환경을 자동 구축합니다. 이 스크립트는 Multipass로 가상 컴퓨터를 만들 때 Ubuntu 22.04 이미지를 인터넷에서 다운로드합니다.

문제는 Multipass가 **항상 최신 이미지**를 받아온다는 점입니다. Canonical이 이미지를 업데이트하면서 보안 기본값을 변경했고, 이전에는 켜져 있던 설정들이 꺼진 상태로 바뀌었습니다.

**쉬운 비유:**
> 건물 설계도(스크립트)는 안 바뀌었는데, 건축 자재(Ubuntu 이미지)의 규격이 바뀐 것.
> 원래 나사가 기본 포함이었는데, 이제는 나사를 따로 조여야 하는 것과 같음.

### 변경사항 9가지 (전부 `demo-setup.sh` 1개 파일에만 적용)

> **중요: 프로젝트의 실제 소스코드(manager, agent, dashboard)는 전혀 변경되지 않았습니다.**
> 변경된 것은 "설치 스크립트"뿐입니다. 프로그램 자체가 아니라 프로그램을 설치하는 절차만 수정한 것입니다.

---

#### 변경 1: Cloud 가상 컴퓨터 메모리 2G → 4G

**뭐가 문제였나?**
Cloud 노드에서 프로그램 이미지를 빌드(만드는)할 때 메모리를 많이 씁니다. 2GB로는 메모리가 부족해서 중앙 관리 프로그램(API 서버)이 강제 종료되었습니다.

**뭘 바꿨나?**
Cloud 가상 컴퓨터의 RAM을 2GB에서 4GB로 늘렸습니다.

```
변경 전: multipass launch 22.04 -n cloud -c 2 -m 2G -d 10G
변경 후: multipass launch 22.04 -n cloud -c 2 -m 4G -d 10G
```

**비유:** 작업대가 좁아서 물건이 자꾸 떨어지니까, 작업대를 넓힌 것.

---

#### 변경 2: 네트워크 브릿지 설정 추가

**뭐가 문제였나?**
Kubernetes 설치 첫 단계에서 "네트워크 브릿지 기능이 꺼져있다"는 에러가 나면서 설치가 중단되었습니다. 이전 Ubuntu 이미지에서는 이 기능이 기본으로 켜져 있었는데, 최신 이미지에서는 보안상 꺼져 있습니다.

**뭘 바꿨나?**
Kubernetes 설치 전에 필요한 커널 설정을 먼저 켜도록 5줄 추가했습니다.

```bash
# 추가된 코드
sudo modprobe br_netfilter
echo br_netfilter | sudo tee /etc/modules-load.d/br_netfilter.conf
echo 'net.bridge.bridge-nf-call-iptables = 1
net.ipv4.ip_forward = 1' | sudo tee /etc/sysctl.d/k8s.conf
sudo sysctl --system
```

**비유:** 공장에 새 장비를 들일 때, 예전에는 전원 콘센트가 있었는데 이제는 직접 전기 공사를 해야 하는 것. 그래서 장비 설치 전에 전기 공사를 먼저 하도록 순서를 추가한 것.

---

#### 변경 3: 설정 폴더 생성 추가

**뭐가 문제였나?**
Kubernetes 설치 후 설정 파일을 복사해야 하는데, 복사할 폴더(`/root/.kube/`)가 존재하지 않아서 복사가 실패했습니다. 그런데 기존 코드에 `|| true`(에러 무시)가 있어서 에러가 눈에 보이지 않았고, 다음 단계에서 "설정 파일을 찾을 수 없다"며 실패했습니다.

**뭘 바꿨나?**
폴더를 먼저 만들고 복사하도록 수정했습니다.

```
변경 전: sudo cp /etc/kubernetes/admin.conf /root/.kube/config 2>/dev/null || true
변경 후: sudo mkdir -p /root/.kube && sudo cp /etc/kubernetes/admin.conf /root/.kube/config
```

**비유:** 택배를 보관함에 넣으려는데 보관함이 없었던 것. 보관함을 먼저 설치하고 택배를 넣도록 바꿈.

---

#### 변경 4: API 서버 준비 대기 로직 추가

**뭐가 문제였나?**
Kubernetes를 설치(`kubeadm init`)한 직후 바로 다음 명령을 실행했는데, 아직 Kubernetes 서버가 완전히 켜지기 전이라 명령이 실패했습니다.

**뭘 바꿨나?**
"서버가 준비될 때까지 기다린 다음 진행"하는 대기 코드를 추가했습니다.

```bash
# 추가된 코드
echo "  API 서버 준비 대기 중..."
for i in $(seq 1 30); do
  kubectl get nodes &>/dev/null && break
  echo '  대기 중...'
  sleep 5
done
```

**비유:** 컴퓨터 전원을 켠 직후 바탕화면이 뜨기도 전에 프로그램을 실행하려는 것과 같음. 바탕화면이 뜰 때까지 기다리도록 바꿈.

---

#### 변경 5: CloudCore 설치에 --force 옵션 추가

**뭐가 문제였나?**
KubeEdge CloudCore를 설치할 때, 설치 프로그램이 "CloudCore가 완전히 시작될 때까지" 기다립니다. 그런데 가상 컴퓨터의 네트워크가 느려서 타임아웃(시간 초과)이 발생했습니다.

**뭘 바꿨나?**
`--force` 옵션을 추가해서 "완전히 시작될 때까지 기다리지 말고, 설치만 하고 넘어가라"고 지시했습니다. 대신 다음 단계(변경 6)에서 별도로 준비 상태를 확인합니다.

```
변경 전: sudo keadm init --advertise-address=$CLOUD_IP --kubeedge-version=1.19.0
변경 후: sudo keadm init --advertise-address=$CLOUD_IP --kubeedge-version=1.19.0 --force
```

**비유:** 택배 기사가 수령인이 올 때까지 무한정 기다리다가 시간 초과로 반송하는 것을 방지. "문 앞에 놓고 가세요"로 바꾸고, 나중에 직접 택배를 확인.

---

#### 변경 6: CloudCore 시작 대기 + 토큰 재시도

**뭐가 문제였나?**
변경 5에서 기다리지 않고 넘어갔으니, CloudCore가 아직 준비되지 않은 상태에서 토큰(Edge 노드가 Cloud에 접속하기 위한 암호 열쇠)을 받으려 하면 빈 값이 나옵니다.

**뭘 바꿨나?**
1. CloudCore가 실행될 때까지 기다리는 코드 추가
2. 토큰 발급을 최대 5번 재시도하는 코드 추가

```bash
# 추가된 코드
echo "  CloudCore Pod 준비 대기 중..."
sleep 30
# CloudCore가 Running 상태가 될 때까지 대기 (최대 5분)
for i in $(seq 1 60); do
  kubectl get pods -n kubeedge | grep '1/1.*Running' && break
  sleep 5
done
# 토큰 발급 (최대 5회 재시도)
for i in 1 2 3 4 5; do
  TOKEN=$(keadm gettoken) && [ -n "$TOKEN" ] && break
  echo "  토큰 재시도 ($i/5)..."
  sleep 10
done
```

**비유:** 은행 창구가 열리기 전에 가서 "비밀번호 재발급해주세요"라고 하면 안 됨. 창구가 열릴 때까지 기다리고, 혹시 실패하면 다시 번호표를 뽑는 것.

---

#### 변경 7: Edge 노드 등록 대기

**뭐가 문제였나?**
Edge 노드가 Cloud에 "저 왔어요"라고 등록하는 데 시간이 걸립니다. 등록이 끝나기 전에 Edge 노드의 네트워크 정보를 조회하면 빈 값이 나와서, 네트워크 설정이 잘못 만들어졌습니다.

**뭘 바꿨나?**
Edge 노드 2대가 모두 등록될 때까지 기다리는 코드를 추가했습니다.

```bash
# 추가된 코드
echo "  Edge 노드 등록 대기 중..."
for i in $(seq 1 30); do
  COUNT=$(kubectl get nodes | grep -c edge)
  [ $COUNT -ge 2 ] && break
  sleep 5
done
```

**비유:** 학생 2명이 출석 체크를 해야 하는데, 아직 1명만 왔을 때 수업을 시작하면 안 됨. 2명 다 올 때까지 기다리는 것.

---

#### 변경 8: 파일 전송 경로 수정

**뭐가 문제였나?**
Git Bash에서 파일 경로를 `/tmp/eam.tar.gz`라고 쓰면, Git Bash 내부에서는 이해하지만 Multipass(Windows 프로그램)에게 전달할 때는 Windows 경로(`C:\Users\...\Temp\eam.tar.gz`)로 변환해야 합니다. 이 변환이 안 되어서 파일 전송이 실패했습니다.

**뭘 바꿨나?**
`cygpath -w` 명령으로 Git Bash 경로를 Windows 경로로 자동 변환하도록 수정했습니다.

```bash
# 추가된 코드
EAM_TAR_WIN="$(cygpath -w /tmp/eam.tar.gz 2>/dev/null || echo '...')"
multipass transfer "$EAM_TAR_WIN" cloud:/tmp/eam.tar.gz
```

**비유:** 한국 주소를 영어로 바꿔서 외국 택배사한테 알려주는 것. "서울시 강남구"를 "Gangnam-gu, Seoul"로 번역해야 배달이 됨.

---

#### 변경 9: K8s 배포 전 API 서버 복구 대기

**뭐가 문제였나?**
Step 6에서 이미지를 빌드할 때 메모리를 많이 써서 API 서버가 잠시 죽었다가 자동 복구됩니다. 그런데 Step 8에서 프로그램 배포를 할 때 아직 복구가 안 끝난 상태여서 배포가 실패했습니다.

**뭘 바꿨나?**
1. 30초 기본 대기
2. API 서버가 응답할 때까지 최대 5분 대기
3. 배포 명령 실패 시 최대 3번 재시도

```bash
# 추가된 코드
echo "  API 서버 안정화 대기 중..."
sleep 30
for i in $(seq 1 60); do
  kubectl get nodes &>/dev/null && break
  sleep 5
done
# namespace 생성 재시도
for attempt in 1 2 3; do
  kubectl apply -f /tmp/namespace.yaml && break
  echo '  API 재시도 대기...'
  sleep 15
done
```

**비유:** 큰 짐을 나르느라 지친 사람한테 바로 다른 일을 시키면 못 함. 잠깐 쉬게 하고, 괜찮은지 확인한 다음 일을 시키는 것.

---

### 변경 요약 표

| # | 뭘 바꿨나 | 왜 바꿨나 | 비유 |
|---|----------|----------|------|
| 1 | 메모리 2G → 4G | 이미지 빌드 시 메모리 부족 | 작업대 확장 |
| 2 | 네트워크 설정 추가 | Ubuntu 기본값 변경 | 전기 공사 추가 |
| 3 | 설정 폴더 생성 | 폴더 없어서 복사 실패 | 보관함 설치 |
| 4 | API 서버 대기 | 서버 준비 전 명령 실행 | 부팅 완료 대기 |
| 5 | --force 옵션 | 네트워크 느려서 타임아웃 | 문 앞 택배 |
| 6 | 토큰 재시도 | CloudCore 미준비 | 은행 창구 대기 |
| 7 | 등록 대기 | Edge 미등록 시 정보 조회 실패 | 출석 체크 |
| 8 | 경로 변환 | Git Bash ↔ Windows 경로 차이 | 주소 번역 |
| 9 | 배포 전 대기 | 이미지 빌드 후 서버 복구 필요 | 쉬는 시간 |

---

## 4. 운용 방법

### 4-1. 최초 설치 (처음 한 번만)

Git Bash를 열고:
```bash
cd ~/PycharmProjects/kubeedge-edge-prototype
bash demo-setup.sh
```

- 약 **10~15분** 소요
- 완료되면 Dashboard 주소와 RabbitMQ 주소가 화면에 표시됨
- 이 주소를 브라우저에 입력하면 웹 화면을 볼 수 있음

### 4-2. 일상적인 사용

#### PC를 켰을 때 (VM 시작)
```bash
multipass start cloud edge1 edge2
```
> VM은 PC를 껐다 켜도 데이터가 유지됩니다. 다시 설치할 필요 없습니다.

#### VM이 정상인지 확인
```bash
multipass list
```
3개 모두 **Running**이면 정상:
```
Name      State       IPv4
cloud     Running     172.x.x.x
edge1     Running     172.x.x.x
edge2     Running     172.x.x.x
```

#### 웹 Dashboard 접속
1. Cloud IP 확인:
   ```bash
   multipass info cloud --format csv | tail -1 | cut -d, -f3
   ```
2. 브라우저에서 `http://[위에서 나온 IP]:30501` 접속

#### RabbitMQ 관리 화면 접속
- 주소: `http://[Cloud IP]:15672`
- ID: `isl`
- PW: `wjdqhqhghdusrntlf1!`

#### 노드/Pod 상태 확인
```bash
# 노드 3대 상태
multipass exec cloud -- kubectl get nodes

# 프로그램(Pod) 상태
multipass exec cloud -- kubectl get pods -n edge-auth -o wide
```
정상이면 모든 노드가 `Ready`, 모든 Pod이 `Running`

#### 통신 테스트 (Edge 노드끼리 메시지 전송)
```bash
cd ~/PycharmProjects/kubeedge-edge-prototype
bash demo-test.sh
```
성공하면:
```
✅ 통신 테스트 성공!
  edge1 → edge2 메시지 전송 OK
  edge2 → edge1 응답 전송 OK
```

#### PC 끄기 전 (VM 정지)
```bash
multipass stop cloud edge1 edge2
```
> 정지만 하면 됩니다. 삭제하지 마세요.

### 4-3. 환경 완전 삭제 (처음부터 다시 할 때)
```bash
cd ~/PycharmProjects/kubeedge-edge-prototype
bash demo-stop.sh
```
> `y`를 입력하면 VM 3대를 모두 삭제합니다.
> 다시 구축하려면 `bash demo-setup.sh`를 실행하면 됩니다.

---

## 5. 트러블슈팅 (문제 해결)

### "Dashboard가 안 열려요"

**확인 순서:**

1. VM이 켜져 있는지 확인:
   ```bash
   multipass list
   ```
   `Stopped`이면 → `multipass start cloud edge1 edge2`

2. Dashboard Pod이 실행 중인지 확인:
   ```bash
   multipass exec cloud -- kubectl get pods -n edge-auth -l app=dashboard
   ```
   `Running`이 아니면 → 재시작:
   ```bash
   multipass exec cloud -- kubectl rollout restart deployment dashboard -n edge-auth
   ```

3. 30초 기다린 후 브라우저에서 다시 접속

### "통신 테스트가 실패해요"

1. 모든 Pod 상태 확인:
   ```bash
   multipass exec cloud -- kubectl get pods -n edge-auth -o wide
   ```

2. `Pending`이나 `Error` 상태의 Pod가 있으면:
   ```bash
   # 전체 재시작
   multipass exec cloud -- kubectl rollout restart deployment -n edge-auth --all
   ```

3. 1분 기다린 후 다시 테스트:
   ```bash
   bash demo-test.sh
   ```

### "multipass list에서 아무것도 안 나와요"

VM이 삭제된 상태입니다. 처음부터 다시 설치해야 합니다:
```bash
bash demo-setup.sh
```

### "VM이 Suspended 상태에요"

PC가 절전 모드에서 깨어날 때 가끔 발생합니다:
```bash
multipass stop cloud edge1 edge2
multipass start cloud edge1 edge2
```

### "demo-setup.sh 실행 중 에러가 났어요"

대부분 네트워크 문제입니다. VM을 삭제하고 다시 시도:
```bash
bash demo-stop.sh       # y 선택
bash demo-setup.sh      # 처음부터 다시
```

### "아무것도 안 되고 모르겠어요"

최후의 수단 — 전부 삭제하고 처음부터:
```bash
multipass stop --all
multipass delete --all
multipass purge
bash demo-setup.sh
```

---

## 6. 자주 쓰는 명령어 모음

| 하고 싶은 것 | 명령어 |
|-------------|--------|
| 환경 처음 구축 | `bash demo-setup.sh` |
| VM 시작 | `multipass start cloud edge1 edge2` |
| VM 정지 | `multipass stop cloud edge1 edge2` |
| VM 상태 확인 | `multipass list` |
| Cloud IP 확인 | `multipass info cloud --format csv \| tail -1 \| cut -d, -f3` |
| 노드 상태 확인 | `multipass exec cloud -- kubectl get nodes` |
| Pod 상태 확인 | `multipass exec cloud -- kubectl get pods -n edge-auth -o wide` |
| 통신 테스트 | `bash demo-test.sh` |
| 환경 삭제 | `bash demo-stop.sh` |
| Dashboard | 브라우저에서 `http://[Cloud IP]:30501` |
| RabbitMQ | 브라우저에서 `http://[Cloud IP]:15672` |

---

## 7. 파일 구조

```
kubeedge-edge-prototype/
├── demo-setup.sh          ← 환경 자동 구축 스크립트 (이 파일만 수정됨)
├── demo-test.sh           ← 통신 테스트 스크립트
├── demo-stop.sh           ← 환경 삭제 스크립트
├── DEMO_GUIDE.md          ← 간단한 사용 가이드
├── TEST_GUIDE.md          ← 상세 테스트 가이드 (노드 추가/삭제 포함)
├── CHANGELOG.md           ← demo-setup.sh 변경 이력 (기술적 상세)
├── FULL_GUIDE.md          ← 이 문서 (완벽 가이드)
├── k8s/                   ← Kubernetes 배포 파일들
│   ├── namespace.yaml
│   ├── rabbitmq.yaml
│   ├── manager.yaml
│   ├── dashboard.yaml
│   ├── agent-edge1.yaml
│   ├── agent-edge2.yaml
│   └── test-comm.yaml
└── README.md              ← 원본 KubeEdge 설치 가이드
```

### 관련 프로젝트 (같은 부모 폴더에 있어야 함)
```
PycharmProjects/
├── kubeedge-edge-prototype/          ← 이 프로젝트 (데모 환경)
└── edge-auth-manager-prototype-ISL/  ← 실제 소스코드 (manager, agent, dashboard)
```

> `demo-setup.sh`는 옆에 있는 `edge-auth-manager-prototype-ISL` 프로젝트의 소스코드를 가져와서 VM 안에서 빌드합니다. 따라서 두 프로젝트가 같은 부모 폴더에 있어야 합니다.

---

## 8. 핵심 정리

1. **변경된 파일은 `demo-setup.sh` 딱 1개**. 프로젝트 소스코드는 변경 없음.
2. **변경 원인은 Ubuntu 이미지 업데이트**. 우리 코드 문제가 아님.
3. **사용자가 실행하는 명령어는 변경 없음**. `bash demo-setup.sh`, `bash demo-test.sh`, `bash demo-stop.sh` 그대로.
4. **PC를 껐다 켜도 VM 데이터는 유지됨**. `multipass start`로 시작만 하면 됨.
5. **문제가 생기면 삭제 후 다시 설치**. `bash demo-stop.sh` → `bash demo-setup.sh`
