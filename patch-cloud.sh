#!/usr/bin/env bash

set -o errexit
set -o nounset
set -o pipefail

source ./tools.sh

function cloudcore_manage_by_systemd() {
    # 云侧cloudcore可以通过systemd管理
    # 拷贝cloudcore.service到/usr/lib/systemd/system
    cp /etc/kubeedge/cloudcore.service /usr/lib/systemd/system

    # 杀掉当前cloudcore进程后重启
    pkill cloudcore
    systemctl restart cloudcore

    # 查看cloudcore运行状态
    systemctl status cloudcore
}

patch_kubeedge_component cloudcore
cloudcore_manage_by_systemd
