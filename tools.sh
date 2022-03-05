#!/usr/bin/env bash

set -o errexit
set -o nounset
set -o pipefail

arch="$(uname -m)"
toolarch=""
tarball_dir=./tarball
config_dir=./config
yamls_dir=./yamls
patch_dir=./patch
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
    local arch=$1
    echo "download crictl tarball: $arch"
    wget -P $tarball_dir --no-check-certificate https://github.com/kubernetes-sigs/cri-tools/releases/download/$crictl_version/crictl-$crictl_version-linux-$arch.tar.gz
}

function clean_crictl_tarball() {
    local arch=$1
    echo "clean crictl tarball: $arch"
    rm -rf $tarball_dir/crictl-$crictl_version-linux-$arch.tar.gz
}

function install_crictl() {
    echo "install crictl"
    tar zxvf $tarball_dir/crictl-$crictl_version-linux-$toolarch.tar.gz -C /usr/local/bin
}

function download_cni_tarball() {
    local arch=$1
    echo "download cni tarball: $arch"
    wget -P $tarball_dir --no-check-certificate https://github.com/containernetworking/plugins/releases/download/$cni_version/cni-plugins-linux-$arch-$cni_version.tgz
}

function clean_cni_tarball() {
    local arch=$1
    echo "clean cni tarball: $arch"
    rm -rf $tarball_dir/cni-plugins-linux-$arch-$cni_version.tgz
}

function install_cni() {
    echo "install cni"
    mkdir -p /opt/cni/bin
    tar -zxvf $tarball_dir/cni-plugins-linux-$toolarch-$cni_version.tgz -C /opt/cni/bin
}

function update_isulad_config() {
    local mode=$1
    echo "update isulad config: $mode"
    echo "$(cat $config_dir/isulad-$mode-daemon.json)" > /etc/isulad/daemon.json
}

function restart_isulad() {
    echo "restart isulad"
    systemctl daemon-reload && systemctl restart isulad
}

function download_flannel_image() {
    local arch=$1
    echo "download flannel image: $arch"
    docker pull --platform=linux/$arch quay.io/coreos/flannel:$flannel_version
    docker save -o $tarball_dir/flannel-$arch.tar quay.io/coreos/flannel:$flannel_version
}

function load_flannel_image() {
    echo "load flannel image"
    isula load -i $tarball_dir/flannel-$toolarch.tar
}

function clean_flannel_image_tarball() {
    local arch=$1
    echo "clean flannel image tarball: $arch"
    rm -rf $tarball_dir/flannel-$arch.tar
}

function install_flannel() {
    local mode=$1
    echo "install flannel: $mode"
    kubectl apply -f $yamls_dir/kube-flannel-$mode.yml
    kubectl wait --timeout=120s --for=condition=Ready pod -l app=flannel -n kube-system
}

function download_kubeedge_pause_image() {
    local arch=$1
    echo "download kubeedge pause image: $arch"
    docker pull --platform=linux/$arch kubeedge/pause:3.1
    docker save -o $tarball_dir/kubeedge-pause-$arch.tar kubeedge/pause:3.1
}

function load_kubeedge_pause_image() {
    echo "load kubeedge pause image"
    isula load -i $tarball_dir/kubeedge-pause-$toolarch.tar
}

function clean_kubeedge_pause_image_tarball() {
    local arch=$1
    echo "clean kubeedge pause image tarball: $arch"
    rm -rf $tarball_dir/kubeedge-pause-$arch.tar
}

function download_nginx_image() {
    local arch=$1
    echo "download nginx image: $arch"
    docker pull --platform=linux/$arch nginx:alpine
    docker save -o $tarball_dir/nginx-$arch.tar nginx:alpine
}

function load_nginx_image() {
    echo "load nginx image"
    isula load -i $tarball_dir/nginx-$toolarch.tar
}

function clean_nginx_image_tarball() {
    local arch=$1
    echo "clean nginx image tarball: $arch"
    rm -rf $tarball_dir/nginx-$arch.tar
}

function patch_kubeedge_component() {
    local ke_component=$1
    patch /etc/kubeedge/config/$ke_component.yaml < $patch_dir/$ke_component.yaml.patch
}
