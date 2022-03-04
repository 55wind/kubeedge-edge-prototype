#!/usr/bin/env bash

set -o errexit
set -o nounset
set -o pipefail

source ./tools.sh

arch_to_toolarch $arch
install_crictl
install_cni
load_flannel_image
install_flannel cloud
