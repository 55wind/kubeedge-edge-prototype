#!/usr/bin/env bash

set -o errexit
set -o nounset
set -o pipefail

source ./tools.sh

arch_to_toolarch $arch
install_crictl
install_cni
update_isulad_config edge
restart_isulad
load_flannel_image
load_kubeedge_pause_image
load_nginx_image
yum install -y kubeedge-keadm kubeedge-edgecore
# 同步时钟，选择可以访问的NTP服务器即可
ntpdate cn.pool.ntp.org
