#!/usr/bin/env bash

set -o errexit
set -o nounset
set -o pipefail

source ./tools.sh

function restart_edgecore() {
    # 重启edgecore
    systemctl restart edgecore

    systemctl status edgecore
}

patch_kubeedge_component edgecore
restart_edgecore
