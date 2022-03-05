#!/usr/bin/env bash

set -o errexit
set -o nounset
set -o pipefail

source ./tools.sh

download_crictl_tarball arm64
download_crictl_tarball amd64

download_cni_tarball arm64
download_cni_tarball amd64

download_flannel_image arm64
download_flannel_image amd64

download_kubeedge_pause_image arm64
download_kubeedge_pause_image amd64

download_nginx_image arm64
download_nginx_image amd64
