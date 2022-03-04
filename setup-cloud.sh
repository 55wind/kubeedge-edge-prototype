#!/usr/bin/env bash

set -o errexit
set -o nounset
set -o pipefail

source ./tools.sh

function prepare_k8s_master_node() {
    # 关闭防火墙
    systemctl stop firewalld
    systemctl disable firewalld

    # 禁用selinux
    setenforce 0

    # 网络配置，开启相应的转发机制
    cat >> /etc/sysctl.d/k8s.conf <<EOF
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

    # 安装k8s工具
    yum install -y kubernetes-master kubernetes-kubeadm kubernetes-client kubernetes-kubelet

    # 开机启动kubelet
    systemctl enable kubelet --now
}

prepare_k8s_master_node
update_isulad_config cloud
restart_isulad
