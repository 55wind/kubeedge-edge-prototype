# KubeEdge Tools

kubeedge-tools 提供了完备的离线安装包以及部署脚本，旨在帮助用户快速搭建 KubeEdge 集群：
- 大大减少操作步骤、简化部署难度
- 能够支持节点无法访问外网的情况

## 当前安装包版本

| 组件        | 版本       |  功能                          |
| ----------  | --------- | ----------------------------- |
| crictl      | v1.20.0   | CRI 兼容的容器运行时命令行接口工具 |
| cni plugins | v0.9.0    | CNI 工具集                     |
| flannel     | v0.14.0   | flannel 镜像用于提供容器网络功能  |
| nginx       | alpine    | nginx 镜像用于测试集群基本功能    |

---

# KubeEdge 部署指南

## 介绍

### KubeEdge

KubeEdge 是一个致力于解决边缘场景问题的开源系统，它将容器化应用程序编排和设备管理的能力扩展到边缘设备。基于 Kubernetes，KubeEdge 为网络、应用程序部署以及云侧与边缘侧之间的元数据同步提供核心基础设施支持。KubeEdge 支持 MQTT，并允许开发人员编写自定义逻辑，在边缘上启用资源受限的设备通信。KubeEdge 由云部分和边缘部分组成，目前均已开源。

>  https://kubeedge.io/

### iSulad

iSulad 是一个轻量级容器 runtime 守护程序，专为 IOT 和 Cloud 基础设施而设计，具有轻便、快速且不受硬件规格和体系结构限制的特性，可以被更广泛地应用在云、IoT、边缘计算等多个场景。

>  https://gitee.com/openeuler/iSulad

## 集群概览

### 组件版本

| 组件       | 版本                              |
| ---------- | --------------------------------- |
| OS         | openEuler 21.09                   |
| Kubernetes | 1.20.2-4                          |
| iSulad     | 2.0.9-20210625.165022.git5a088d9c |
| KubeEdge   | v1.8.0                            |

### 节点规划（示例）

| 节点         | 位置          | 组件                             |
| ------------ | ------------- | -------------------------------- |
| 192.168.56.8 | 云侧（cloud） | k8s（master）、isulad、cloudcore |
| 192.168.56.9 | 边缘侧（edge）  | isulad、edgecore                 |

## 准备

### 下载工具包

[kubeedge-tools](https://gitee.com/Poorunga/kubeedge-tools) 工具包提供了完备的离线安装包以及部署脚本，降低了部署复杂度并且支持在节点无法访问外网的条件下快速搭建 KubeEdge 集群。

```bash
# 下载 kubeedge-tools 工具包并解压（包括云侧和边缘侧）
$ wget -O kubeedge-tools.zip https://gitee.com/Poorunga/kubeedge-tools/repository/archive/master.zip
$ unzip kubeedge-tools.zip

# 进入 kubeedge-tools 工具包目录（后续所有操作基于此目录）
$ cd kubeedge-tools-master
```

### 部署 k8s

以下操作仅在云侧执行

#### 初始化云侧环境

```bash
$ ./setup-cloud.sh
```

#### 参考 [Kubernetes 集群部署指南](https://docs.openeuler.org/zh/docs/21.09/docs/Kubernetes/Kubernetes.html) 部署 k8s

> 提示：在云侧节点可以访问外网的条件下建议优先选用 `kubeadm` 工具部署 k8s，示例：

```bash
$ kubeadm init --apiserver-advertise-address=192.168.56.8 --kubernetes-version v1.20.11 --pod-network-cidr=10.244.0.0/16 --upload-certs --cri-socket=/var/run/isulad.sock
...
Your Kubernetes control-plane has initialized successfully!
...
```

#### 安装云侧容器网络

目前有丰富的 cni 软件可以为 k8s 节点提供容器网络功能，比如 [flannel](https://github.com/flannel-io/flannel)、[calico](https://github.com/projectcalico/calico)、[cilium](https://github.com/cilium/cilium) 等，如果你暂时不明确选用哪款 cni 软件，可以使用下方命令安装云侧容器网络：

```bash
$ ./install-flannel-cloud.sh
```

#### 检查部署情况

```bash
# 查看节点状态（Ready 即正常）
$ kubectl get nodes
NAME             STATUS   ROLES                  AGE   VERSION
cloud.kubeedge   Ready    control-plane,master   12m   v1.20.2

# 查看所有 k8s 组件运行状态（Running 即正常）
$ kubectl get pods -n kube-system
NAME                                     READY   STATUS    RESTARTS   AGE
coredns-74ff55c5b-4ptkh                  1/1     Running   0          15m
coredns-74ff55c5b-zqx5n                  1/1     Running   0          15m
etcd-cloud.kubeedge                      1/1     Running   0          15m
kube-apiserver-cloud.kubeedge            1/1     Running   0          15m
kube-controller-manager-cloud.kubeedge   1/1     Running   0          15m
kube-flannel-cloud-ds-lvh4n              1/1     Running   0          13m
kube-proxy-2tcnn                         1/1     Running   0          15m
kube-scheduler-cloud.kubeedge            1/1     Running   0          15m
```

## 部署

### 部署 cloudcore

以下操作仅在云侧执行

#### 初始化集群

```bash
# --advertise-address 填写云侧 IP
$ keadm init --advertise-address="192.168.56.8" --kubeedge-version=1.8.0
...
CloudCore started
```

#### 调整 cloudcore 配置

```bash
$ ./patch-cloud.sh
```

#### 检查部署情况

```bash
# active (running)即正常
$ systemctl status cloudcore | grep running
     Active: active (running) since Fri 2022-03-04 10:54:30 CST; 5min ago
```

至此，云侧的 cloudcore 已部署完成，接下来部署边缘侧 edgecore。

### 部署 edgecore

以下命令如无特殊说明则仅在边缘侧执行

#### 初始化边缘侧环境

```bash
$ ./setup-edge.sh
```

#### 纳管边缘节点

```bash
# keadm gettoken 命令需要在云侧执行
$ keadm gettoken
96058ab80ffbeb87fe58a79bfb19ea13f9a5a6c3076a17c00f80f01b406b4f7c.eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJleHAiOjE2NDY0NDg4NzF9.1mJegWB7SUVjgf-OvAqILgbZXeMHR9eOzMxpNFc42SI

# keadm join 命令在边缘侧执行
# --cloudcore-ipport 填写云侧 IP:10000；--token 填写上方 token 值
$ keadm join --cloudcore-ipport=192.168.56.8:10000 --kubeedge-version=1.8.0 --token=96058ab80ffbeb87fe58a79bfb19ea13f9a5a6c3076a17c00f80f01b406b4f7c.eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJleHAiOjE2NDY0NDg4NzF9.1mJegWB7SUVjgf-OvAqILgbZXeMHR9eOzMxpNFc42SI
...
KubeEdge edgecore is running...
```

#### 调整 edgecore 配置

```bash
$ ./patch-edge.sh
```

#### 安装边缘侧容器网络

如果你暂时不明确选用哪款 cni 软件，可以使用下方命令安装边缘侧容器网络：

```bash
# 下方命令需要在云侧执行
$ ./install-flannel-edge.sh
```

#### 检查边缘节点是否纳管成功

```bash
# 下方命令需要在云侧执行（发现已经有了边缘节点）
$ kubectl get nodes
NAME             STATUS   ROLES                  AGE     VERSION
cloud.kubeedge   Ready    control-plane,master   1h      v1.20.2
edge.kubeedge    Ready    agent,edge             10m     v1.19.3-kubeedge-v1.8.0
```

至此，KubeEdge 集群部署完成，接下来我们测试一下从云侧下发应用到边缘侧。

### 部署应用

以下命令在云侧执行

#### 部署nginx

```bash
$ kubectl apply -f yamls/nginx-deployment.yaml
deployment.apps/nginx-deployment created

# 查看应用是否部署到了边缘侧（Running 即正常）
$ kubectl get pod -owide | grep nginx
nginx-deployment-84b99f4bf-jb6sz   1/1     Running   0          30s   10.244.1.2   edge.kubeedge   <none>           <none>
```

#### 测试功能

```bash
# 进入边缘侧节点，访问 nginx 应用
$ curl 10.244.1.2:80
<!DOCTYPE html>
<html>
<head>
<title>Welcome to nginx!</title>
<style>
html { color-scheme: light dark; }
body { width: 35em; margin: 0 auto;
font-family: Tahoma, Verdana, Arial, sans-serif; }
</style>
</head>
<body>
<h1>Welcome to nginx!</h1>
<p>If you see this page, the nginx web server is successfully installed and
working. Further configuration is required.</p>

<p>For online documentation and support please refer to
<a href="http://nginx.org/">nginx.org</a>.<br/>
Commercial support is available at
<a href="http://nginx.com/">nginx.com</a>.</p>

<p><em>Thank you for using nginx.</em></p>
</body>
</html>
```

至此，KubeEdge 部署已经全流程打通。
