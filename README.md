# KubeEdge Tools (Ubuntu + containerd 전용 가이드)

이 저장소는 **Ubuntu + containerd + kubeadm + keadm** 환경 기준으로 맞춰졌습니다.

## 원문 링크 + 출처 표기

- 원문 제목: KubeEdge Deployment Guide
- 원문 링크: https://docs.openeuler.org/en/docs/24.03_LTS_SP1/edge_computing/kube_edge/kube_edge_deployment_guide.html#cluster-overview
- 출처: openEuler community
- 수정 여부: **수정했다**
- 수정 요약: Ubuntu + containerd + kubeadm + keadm 환경으로 변경했고, 내 IP/스크립트로 튜닝했다.

---

## 1) 환경

- Cloud: `etri-system-product-name` (`192.168.0.56`)
- Edge: `jetson-desktop` (`192.168.0.3`), `rpi-worker-1` (`192.168.0.4`)
- Runtime: `containerd`
- Kubernetes: `kubeadm`
- KubeEdge: `keadm` (기본 `v1.22.0`)

필요 시 버전 변경:

```bash
export KUBEEDGE_VERSION=v1.22.0
```

---

## 2) Cloud 설치

```bash
cd /home/etri/jinuk/kubeedge-tools
sudo ./setup-cloud.sh
```

### kubeadm init (아직 안 했으면)

```bash
sudo kubeadm init \
  --apiserver-advertise-address=192.168.0.56 \
  --pod-network-cidr=10.244.0.0/16

mkdir -p $HOME/.kube
sudo cp /etc/kubernetes/admin.conf $HOME/.kube/config
sudo chown $(id -u):$(id -g) $HOME/.kube/config
```
### 마스터 테인트 제거
```bash
kubectl taint nodes --all node-role.kubernetes.io/control-plane-
```
### CloudCore 초기화

```bash
sudo keadm init --advertise-address=192.168.0.56 --kubeedge-version=1.22.0
sudo ./patch-cloud.sh
sudo ./install-flannel-cloud.sh
```

cloud core pending에서 안넘어갈시 직접 파드 delete

### cloudcore Deployment에 control-plane 고정 패치
```bash
kubectl -n kubeedge patch deploy cloudcore --type='merge' -p '{
  "spec": {
    "template": {
      "spec": {
        "nodeSelector": {
          "node-role.kubernetes.io/control-plane": ""
        },
        "tolerations": [
          {
            "key": "node-role.kubernetes.io/control-plane",
            "operator": "Exists",
            "effect": "NoSchedule"
          }
        ]
      }
    }
  }
}'

```
---

## 3) Edge 설치 (각 노드 반복)

```bash
sudo ./setup-edge.sh
```

Cloud에서 토큰 발급:

```bash
keadm gettoken
```

Edge에서 조인:

```bash
sudo keadm join \
  --cloudcore-ipport=192.168.0.56:10000 \
  --token=<TOKEN> \
  --kubeedge-version=1.22.0

sudo ./patch-edge.sh
```

---

## 4) Edge Flannel 배포 (Cloud에서 실행)

```bash
cd /home/etri/jinuk/kubeedge-tools
sudo ./install-flannel-edge.sh
```

> `install-flannel-edge.sh`는 반드시 Cloud 노드에서 실행.

---

## 5) 확인

```bash
kubectl get nodes -o wide
kubectl get pods -A -o wide
kubectl get ds -A -o wide
```

정상 기준:
- cloud/edge 노드 `Ready`
- `kube-flannel-cloud-ds`, `kube-flannel-edge-ds` 정상
- `cloudcore` `Running`

---

## 6) kubectl logs/exec 디버그 활성화 (keadm 환경)
https://kubeedge.io/docs/advanced/debug/

---

## 7) 출처

- OpenEuler KubeEdge Deployment Guide: https://docs.openeuler.org/en/docs/24.03_LTS_SP1/edge_computing/kube_edge/kube_edge_deployment_guide.html#cluster-overview
- KubeEdge Advanced Debug: https://kubeedge.io/docs/advanced/debug/

---

## 8) 라이선스

- 문서(README, `docs/`): **CC BY-SA 4.0**
  - https://creativecommons.org/licenses/by-sa/4.0/
  - 본 문서 및 파생 문서는 동일조건(CC BY-SA 4.0)으로 배포
- 코드/스크립트(그 외 파일): 루트 [LICENSE](LICENSE)의 Apache-2.0 적용

참고:
- [docs/ATTRIBUTION.md](docs/ATTRIBUTION.md)
- [docs/LICENSE-CC-BY-SA-4.0.md](docs/LICENSE-CC-BY-SA-4.0.md)
