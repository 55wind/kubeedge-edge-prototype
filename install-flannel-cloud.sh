#!/usr/bin/env bash

set -o errexit
set -o nounset
set -o pipefail

source ./tools.sh

arch_to_toolarch $arch
load_flannel_image
install_flannel cloud
