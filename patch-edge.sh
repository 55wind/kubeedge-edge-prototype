#!/usr/bin/env bash

set -o errexit
set -o nounset
set -o pipefail

source ./tools.sh

function patch_edgecore_config() {
		local cfg="/etc/kubeedge/config/edgecore.yaml"
		if [[ ! -f "$cfg" ]]; then
				echo "edgecore config not found: $cfg"
				exit 1
		fi

		local runtime_sock="unix:///run/containerd/containerd.sock"

		# CRI endpoint/runtimeType를 현재 환경(containerd)에 맞춰 교정
		sed -i -E "s#(^[[:space:]]*remoteImageEndpoint:[[:space:]]*).*#\\1${runtime_sock}#" "$cfg" || true
		sed -i -E "s#(^[[:space:]]*remoteRuntimeEndpoint:[[:space:]]*).*#\\1${runtime_sock}#" "$cfg" || true
		sed -i -E "s#(^[[:space:]]*runtimeType:[[:space:]]*).*#\\1remote#" "$cfg" || true

		# metaServer.enable: false -> true (edge flannel/메타서버 경로 사용)
		awk '
			BEGIN { in_meta=0 }
			{
				if ($0 ~ /^[[:space:]]*metaServer:[[:space:]]*$/) {
					in_meta=1
					print
					next
				}
				if (in_meta==1 && $0 ~ /^[[:space:]]*enable:[[:space:]]*false[[:space:]]*$/) {
					sub(/false/, "true")
					in_meta=0
					print
					next
				}
				if (in_meta==1 && $0 !~ /^[[:space:]]+/) {
					in_meta=0
				}
				print
			}
		' "$cfg" > "${cfg}.tmp"
		mv "${cfg}.tmp" "$cfg"
}

patch_edgecore_config
systemctl restart edgecore
systemctl status edgecore --no-pager -n 60
