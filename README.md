# KubeEdge Tools (Ubuntu + containerd 전용 가이드)

이 저장소는 **Ubuntu + containerd + kubeadm + keadm** 환경 기준으로 맞춰졌습니다.

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

### CloudCore 초기화

```bash
sudo keadm init --advertise-address=192.168.0.56 --kubeedge-version=1.22.0
sudo ./patch-cloud.sh
sudo ./install-flannel-cloud.sh
```

---

## 3) Edge 설치 (각 노드 반복)

```bash
cd /home/etri/jinuk/kubeedge-tools
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

- OpenEuler KubeEdge Deployment Guide (Installing Kubernetes): https://docs.openeuler.org/en/docs/24.03_LTS_SP1/edge_computing/kube_edge/kube_edge_deployment_guide.html?utm_source=chatgpt.com#user-content-installing-kubernetes
- KubeEdge Advanced Debug: https://kubeedge.io/docs/advanced/debug/
