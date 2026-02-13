#!/usr/bin/env bash

set -o errexit
set -o nounset
set -o pipefail

source ./tools.sh

KUBEEDGE_VERSION="${KUBEEDGE_VERSION:-v1.22.0}"

function ensure_root() {
    if [[ "$(id -u)" -ne 0 ]]; then
        echo "please run as root: sudo ./setup-cloud.sh"
        exit 1
    fi
}

function install_keadm_binary() {
    if command -v keadm >/dev/null 2>&1; then
        echo "keadm already installed: $(keadm version 2>/dev/null || true)"
        return
    fi

    local arch=$1
    local pkg_arch="amd64"
    if [[ "$arch" == "arm64" ]]; then
        pkg_arch="arm64"
    fi

    local url="https://github.com/kubeedge/kubeedge/releases/download/${KUBEEDGE_VERSION}/keadm-${KUBEEDGE_VERSION}-linux-${pkg_arch}.tar.gz"
    local tmp="/tmp/keadm-${KUBEEDGE_VERSION}-linux-${pkg_arch}.tar.gz"

    echo "download keadm: $url"
    wget -O "$tmp" "$url"
    tar -xzf "$tmp" -C /tmp
    install -m 0755 /tmp/keadm/keadm /usr/local/bin/keadm
    rm -rf /tmp/keadm "$tmp"
}

function prepare_k8s_master_node_common() {
    # 网络配置，开启相应的转发机制
    cat > /etc/sysctl.d/k8s.conf <<EOF
net.bridge.bridge-nf-call-ip6tables = 1
net.bridge.bridge-nf-call-iptables = 1
net.ipv4.ip_forward = 1
vm.swappiness=0
EOF

    # 生效规则
    modprobe br_netfilter
    sysctl -p /etc/sysctl.d/k8s.conf

    # 查看是否生效
    cat /proc/sys/net/bridge/bridge-nf-call-ip6tables
    cat /proc/sys/net/bridge/bridge-nf-call-iptables

    # 关闭系统 swap
    swapoff -a
}

function prepare_k8s_master_node_yum() {
    # 关闭防火墙
    systemctl stop firewalld || true
    systemctl disable firewalld || true

    # 禁用 selinux
    setenforce 0 || true

    prepare_k8s_master_node_common

    # 安装 k8s 工具
    yum install -y kubernetes-master kubernetes-kubeadm kubernetes-client kubernetes-kubelet

    # 开机启动 kubelet
    systemctl enable kubelet --now
}

function prepare_k8s_master_node_apt() {
    # Ubuntu/Debian: firewalld/selinux 通常不存在，忽略即可
    systemctl stop firewalld 2>/dev/null || true
    systemctl disable firewalld 2>/dev/null || true

    prepare_k8s_master_node_common

    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
        apt-transport-https ca-certificates curl gnupg lsb-release \
        conntrack socat ebtables ethtool ipset ntpdate

    if ! command -v kubelet >/dev/null 2>&1 || ! command -v kubeadm >/dev/null 2>&1 || ! command -v kubectl >/dev/null 2>&1; then
        echo "kubelet/kubeadm/kubectl not found."
        echo "please install Kubernetes packages for your distro first, then rerun this script."
        exit 1
    fi

    systemctl enable kubelet --now || true
}

function prepare_k8s_master_node() {
    if command -v yum >/dev/null 2>&1; then
        prepare_k8s_master_node_yum
    elif command -v apt-get >/dev/null 2>&1; then
        prepare_k8s_master_node_apt
    else
        echo "unsupported package manager. need yum or apt-get"
        exit 1
    fi
}

ensure_root
arch_to_toolarch $arch
install_crictl
install_cni
prepare_k8s_master_node
install_keadm_binary "$toolarch"
echo "waiting for ntpdate, you can shutdown it by Ctrl+C"
echo "ntpdate cn.pool.ntp.org"
ntpdate cn.pool.ntp.org
echo "Done!"
