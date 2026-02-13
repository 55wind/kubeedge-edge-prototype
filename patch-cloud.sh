#!/usr/bin/env bash

set -o errexit
set -o nounset
set -o pipefail

source ./tools.sh

if [[ -z "${KUBECONFIG:-}" && -f /etc/kubernetes/admin.conf ]]; then
    export KUBECONFIG=/etc/kubernetes/admin.conf
fi

function patch_cloudcore_file_mode() {
        patch_kubeedge_component cloudcore

        # 云侧 cloudcore 可以通过 systemd 管理（旧安装方式）
        if [[ -f /etc/kubeedge/cloudcore.service ]]; then
                rm -rf /usr/lib/systemd/system/cloudcore.service
                cp /etc/kubeedge/cloudcore.service /usr/lib/systemd/system
                pkill cloudcore || true
                systemctl daemon-reload
                systemctl enable cloudcore
                systemctl restart cloudcore
        fi
}

function patch_cloudcore_configmap_mode() {
        local ns="kubeedge"
        local cm="cloudcore"
        local tmp_in="/tmp/cloudcore.yaml.in"
        local tmp_out="/tmp/cloudcore.yaml.out"

        kubectl get cm "$cm" -n "$ns" -o jsonpath='{.data.cloudcore\.yaml}' > "$tmp_in"

        awk '
            BEGIN { in_dynamic=0 }
            {
                if ($0 ~ /^[[:space:]]*dynamicController:[[:space:]]*$/) {
                    in_dynamic=1
                    print
                    next
                }
                if (in_dynamic==1 && $0 ~ /^[[:space:]]*enable:[[:space:]]*false[[:space:]]*$/) {
                    sub(/false/, "true")
                    in_dynamic=0
                    print
                    next
                }
                if (in_dynamic==1 && $0 !~ /^[[:space:]]+/) {
                    in_dynamic=0
                }
                print
            }
        ' "$tmp_in" > "$tmp_out"

        kubectl create configmap "$cm" -n "$ns" \
            --from-file=cloudcore.yaml="$tmp_out" \
            -o yaml --dry-run=client | kubectl apply -f -

        # cloudcore uses hostPorts, so rolling update can cause port conflicts.
        kubectl patch deploy/cloudcore -n "$ns" --type='json' \
            -p='[{"op":"remove","path":"/spec/strategy/rollingUpdate"}]' >/dev/null 2>&1 || true
        kubectl patch deploy/cloudcore -n "$ns" \
            -p='{"spec":{"strategy":{"type":"Recreate"}}}' >/dev/null 2>&1 || true

        kubectl rollout restart deploy/cloudcore -n "$ns"
        kubectl rollout status deploy/cloudcore -n "$ns" --timeout=180s
}

if [[ -f /etc/kubeedge/config/cloudcore.yaml ]]; then
        patch_cloudcore_file_mode
elif kubectl get cm cloudcore -n kubeedge >/dev/null 2>&1; then
        patch_cloudcore_configmap_mode
else
        echo "cloudcore config not found in file mode or configmap mode"
        echo "- missing /etc/kubeedge/config/cloudcore.yaml"
        echo "- missing configmap kubeedge/cloudcore"
        exit 1
fi
