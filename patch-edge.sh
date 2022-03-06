#!/usr/bin/env bash

set -o errexit
set -o nounset
set -o pipefail

source ./tools.sh

patch_kubeedge_component edgecore
systemctl restart edgecore
systemctl status edgecore
