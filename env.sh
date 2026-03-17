#!/usr/bin/env bash
# KubeEdge 환경 설정 파일 - 본인 환경에 맞게 수정하세요

# Cloud(Master) 노드 IP
export CLOUD_IP="192.168.0.56"

# Edge 노드 IP 목록
export EDGE1_IP="192.168.0.3"    # jetson-desktop
export EDGE2_IP="192.168.0.4"    # rpi-worker-1

# KubeEdge 버전
export KUBEEDGE_VERSION="v1.22.0"

# Pod Network CIDR (flannel 기본값)
export POD_NETWORK_CIDR="10.244.0.0/16"
