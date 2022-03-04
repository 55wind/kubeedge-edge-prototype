#!/usr/bin/env bash

set -o errexit
set -o nounset
set -o pipefail

arch="$(uname -m)"
toolarch=""
crictl_version=v1.20.0
cni_version=v0.9.0
flannel_version=v0.14.0

function arch_to_toolarch() {
    case $1 in
    x86_64)
        toolarch="amd64"
        ;;
    aarch64)
        toolarch="arm64"
        ;;
    *)
        echo "unsupported arch: $arch [x86_64|aarch64]"
        exit 1
        ;;
    esac
    echo "get toolarch: $toolarch"
}

function download_crictl_tarball() {
    arch=$1
    echo "download crictl tarball: $arch"
    wget --no-check-certificate https://github.com/kubernetes-sigs/cri-tools/releases/download/$crictl_version/crictl-$crictl_version-linux-$arch.tar.gz
}

function clean_crictl_tarball() {
    arch=$1
    echo "clean crictl tarball: $arch"
    rm -rf crictl-$crictl_version-linux-$arch.tar.gz
}

function install_crictl() {
    echo "install crictl"
    tar zxvf crictl-$crictl_version-linux-$toolarch.tar.gz -C /usr/local/bin
}

function download_cni_tarball() {
    arch=$1
    echo "download cni tarball: $arch"
    wget --no-check-certificate https://github.com/containernetworking/plugins/releases/download/$cni_version/cni-plugins-linux-$arch-$cni_version.tgz
}

function clean_cni_tarball() {
    arch=$1
    echo "clean cni tarball: $arch"
    rm -rf cni-plugins-linux-$arch-$cni_version.tgz
}

function install_cni() {
    echo "install cni"
    mkdir -p /opt/cni/bin
    tar -zxvf cni-plugins-linux-$toolarch-$cni_version.tgz -C /opt/cni/bin
}

function update_isulad_config() {
    mode=$1
    echo "update isulad config: $mode"
    echo "$(cat ./isulad-$mode-daemon.json)" > /etc/isulad/daemon.json
}

function restart_isulad() {
    echo "restart isulad"
    systemctl daemon-reload && systemctl restart isulad
}

function download_flannel_image() {
    arch=$1
    echo "download flannel image: $arch"
    docker pull --platform=linux/$arch quay.io/coreos/flannel:$flannel_version
    docker save -o flannel-$arch.tar quay.io/coreos/flannel:$flannel_version
}

function load_flannel_image() {
    echo "load flannel image"
    isula load -i flannel-$toolarch.tar
}

function clean_flannel_image_tarball() {
    arch=$1
    echo "clean flannel image tarball: $arch"
    rm -rf flannel-$arch.tar
}

function install_flannel() {
    mode=$1
    echo "install flannel: $mode"
    kubectl apply -f ./kube-flannel-$mode.yml
    kubectl wait --timeout=120s --for=condition=Ready pod -l app=flannel -n kube-system
}
