#!/usr/bin/env bash

set -o errexit
set -o nounset
set -o pipefail

source ./tools.sh

clean_crictl_tarball arm64
clean_crictl_tarball amd64

clean_cni_tarball arm64
clean_cni_tarball amd64

clean_flannel_image_tarball arm64
clean_flannel_image_tarball amd64

clean_kubeedge_pause_image_tarball arm64
clean_kubeedge_pause_image_tarball amd64

clean_nginx_image_tarball arm64
clean_nginx_image_tarball amd64
