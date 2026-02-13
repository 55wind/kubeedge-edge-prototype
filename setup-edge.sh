#!/usr/bin/env bash

set -o errexit
set -o nounset
set -o pipefail

source ./tools.sh

KUBEEDGE_VERSION="${KUBEEDGE_VERSION:-v1.22.0}"

function ensure_root() {
	if [[ "$(id -u)" -ne 0 ]]; then
		echo "please run as root: sudo ./setup-edge.sh"
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

ensure_root
arch_to_toolarch $arch
install_crictl
install_cni
load_flannel_image
load_kubeedge_pause_image
load_nginx_image

if command -v apt-get >/dev/null 2>&1; then
	apt-get update
	DEBIAN_FRONTEND=noninteractive apt-get install -y ntpdate
elif command -v yum >/dev/null 2>&1; then
	yum install -y ntpdate
fi

install_keadm_binary "$toolarch"
echo "waiting for ntpdate, you can shutdown it by Ctrl+C"
echo "ntpdate cn.pool.ntp.org"
ntpdate cn.pool.ntp.org
echo "Done!"
