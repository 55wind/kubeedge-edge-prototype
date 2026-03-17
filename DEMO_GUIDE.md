# KubeEdge 데모 가이드 (비전공자용)

## 이 데모는 무엇인가요?

엣지 컴퓨팅 환경을 PC 한 대에서 시뮬레이션합니다.

```
                    ┌─────────────────┐
                    │   Cloud 노드     │
                    │  (관리 서버)      │
                    │                  │
                    │  Manager (인증)   │
                    │  RabbitMQ (통신)  │
                    │  Dashboard (UI)  │
                    └───────┬──────────┘
                            │ 인터넷
                  ┌─────────┴─────────┐
            ┌─────┴─────┐       ┌─────┴─────┐
            │  Edge 1    │       │  Edge 2    │
            │ (공장 A)   │       │ (공장 B)   │
            │ 온도 센서   │       │ 습도 센서   │
            └───────────┘       └───────────┘
```

- **Edge 노드**: 공장 현장에 설치된 소형 컴퓨터 (여기서는 가상 머신)
- **Cloud 노드**: 중앙 관리 서버
- 두 Edge 노드가 Cloud를 통해 서로 메시지를 주고받습니다

---

## 준비물

| 항목 | 설명 |
|------|------|
| Windows 10/11 PC | RAM 8GB 이상 |
| Hyper-V | Windows 설정에서 활성화 필요 |
| Multipass | VM 관리 도구 ([설치 링크](https://multipass.run/install)) |
| Git Bash | Git for Windows에 포함 ([설치 링크](https://git-scm.com/download/win)) |

### Hyper-V 활성화 방법
1. PowerShell을 **관리자 권한**으로 실행
2. 아래 명령어 입력:
   ```
   Enable-WindowsOptionalFeature -Online -FeatureName Microsoft-Hyper-V -All
   ```
3. PC 재부팅

### Multipass 설치
1. https://multipass.run/install 에서 Windows 버전 다운로드
2. 설치 후 Git Bash에서 `multipass version` 으로 확인

---

## 사용 방법

### 1단계: 환경 구축 (최초 1회, 약 10~15분)

Git Bash를 열고 프로젝트 폴더로 이동합니다:

```bash
cd ~/PycharmProjects/kubeedge-edge-prototype
bash demo-setup.sh
```

완료되면 접속 정보가 표시됩니다.

### 2단계: Dashboard 확인

브라우저에서 화면에 표시된 Dashboard 주소를 엽니다:
- 보통 `http://172.x.x.x:30501`
- 등록된 디바이스와 센서 데이터를 볼 수 있습니다

### 3단계: 통신 테스트

```bash
bash demo-test.sh
```

이 스크립트는:
1. Edge1에서 Edge2로 메시지 5개를 보냅니다
2. Edge2가 받은 메시지에 대해 응답합니다
3. Edge1이 응답을 확인합니다
4. 결과를 화면에 출력합니다

### 4단계: 환경 종료

```bash
bash demo-stop.sh
```

---

## 자주 묻는 질문

### Q: 오래 걸려요
A: 최초 구축 시 VM 생성과 이미지 빌드로 10~15분 걸립니다. 이후에는 VM을 재시작만 하면 됩니다.

### Q: VM을 종료했다가 다시 시작하려면?
```bash
multipass start cloud edge1 edge2
```

### Q: Dashboard가 안 열려요
1. VM이 실행 중인지 확인: `multipass list`
2. Cloud IP 확인: `multipass info cloud`
3. 브라우저에서 `http://[Cloud IP]:30501` 접속

### Q: 테스트가 실패해요
```bash
# Pod 상태 확인
multipass exec cloud -- kubectl get pods -n edge-auth

# 모든 Pod이 Running인지 확인 후 다시 테스트
bash demo-test.sh
```

### Q: 처음부터 다시 하려면?
```bash
bash demo-stop.sh    # y 선택하여 VM 삭제
bash demo-setup.sh   # 다시 구축
```

---

## RabbitMQ 관리 화면

브라우저에서 `http://[Cloud IP]:15672` 접속
- ID: `isl`
- PW: `wjdqhqhghdusrntlf1!`

Queues 탭에서 `agent.metadata` 큐를 클릭하면 실시간으로 쌓이는 센서 데이터를 볼 수 있습니다.
