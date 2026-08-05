"""Capture REAL Kubernetes cluster screens for the ETRI deliverable deck.

This script assumes a REAL, already-running local Kubernetes cluster with the
repo's actual manifests deployed to it (namespace edge-auth: manager,
agent-001, agent-002, gateway-001) plus the official Kubernetes Dashboard.
It does not synthesize any output -- every PNG is a Playwright (real
Chromium) screenshot of either:
  (a) genuine ``kubectl`` stdout, pasted verbatim into an HTML <pre> block
      and screenshotted (k8s_pods.png, k8s_agent_log.png), or
  (b) the live Kubernetes Dashboard web UI, logged into with a real
      ServiceAccount token and screenshotted after navigating to the
      edge-auth namespace (k8s_dashboard.png).

No secret VALUES are ever captured: the pods/svc/secret listing only shows
NAMES and TYPES (kubectl get secret,svc -- never -o yaml).

--------------------------------------------------------------------------
Reproducing the cluster from scratch (documented for the deck; the commands
below were the ones actually used to build the "etri-edge" kind cluster this
script's screenshots were taken against):

  # 1. Install kind (Windows amd64), no system-wide install required:
  curl -fsSL -o kind.exe https://kind.sigs.k8s.io/dl/v0.27.0/kind-windows-amd64

  # 2. Create a 2-node cluster (1 control-plane + 1 worker):
  cat > kind-config.yaml <<'EOF'
  kind: Cluster
  apiVersion: kind.x-k8s.io/v1alpha4
  name: etri-edge
  nodes:
    - role: control-plane
    - role: worker
  EOF
  kind.exe create cluster --config kind-config.yaml --wait 120s

  # 3. Label the worker node to emulate the KubeEdge edge role so the repo's
  #    real manifests (nodeSelector: node-role.kubernetes.io/edge) schedule
  #    unmodified:
  kubectl label node etri-edge-worker node-role.kubernetes.io/edge="" --overwrite

  # 4. Build the repo's real images and load them into kind (imagePullPolicy: Never):
  docker build -f deploy/Dockerfile.manager -t eam-manager:v2 .
  docker build -f deploy/Dockerfile.agent   -t eam-agent:v2   .
  kind.exe load docker-image eam-manager:v2 eam-agent:v2 --name etri-edge

  # 5. Copy k8s/*.yaml to a TEMP dir (repo files are never edited) and
  #    substitute __CLOUD_IP__ with the control-plane node's internal IP
  #    (kubectl get node -o wide) -- inside kind, agents reach the manager's
  #    hostNetwork NodePort 30443 via that address:
  CLOUD_IP=$(kubectl get node etri-edge-control-plane -o jsonpath='{.status.addresses[0].address}')
  for f in namespace.yaml manager.yaml agent-edge1.yaml agent-edge2.yaml gateway.yaml; do
    sed "s/__CLOUD_IP__/$CLOUD_IP/g" k8s/$f > /tmp/k8s-deploy/$f
  done

  # 6. Generate the demo Secret exactly as deploy/demo-setup-v2.sh does
  #    (bootstrap-token / admin-username / admin-password / operator-username
  #    / operator-password via openssl rand), then apply everything:
  kubectl apply -f /tmp/k8s-deploy/namespace.yaml
  kubectl create secret generic eam-secrets -n edge-auth \\
    --from-literal=bootstrap-token=$(openssl rand -hex 32) \\
    --from-literal=admin-username=admin \\
    --from-literal=admin-password=$(openssl rand -base64 24) \\
    --from-literal=operator-username=operator \\
    --from-literal=operator-password=$(openssl rand -base64 24)
  kubectl apply -f /tmp/k8s-deploy/manager.yaml
  kubectl apply -f /tmp/k8s-deploy/agent-edge1.yaml
  kubectl apply -f /tmp/k8s-deploy/agent-edge2.yaml
  kubectl apply -f /tmp/k8s-deploy/gateway.yaml

  # 7. Kubernetes Dashboard + admin token for k8s_dashboard.png:
  kubectl apply -f https://raw.githubusercontent.com/kubernetes/dashboard/v2.7.0/aio/deploy/recommended.yaml
  kubectl apply -f dashboard-admin.yaml   # ServiceAccount eam-admin + cluster-admin binding
  kubectl -n kubernetes-dashboard create token eam-admin --duration=24h
  kubectl -n kubernetes-dashboard port-forward svc/kubernetes-dashboard 8443:443

Run this script itself with:  python ppt/capture_k8s_screens.py
Output: docs/screens/k8s_pods.png, docs/screens/k8s_agent_log.png,
        docs/screens/k8s_dashboard.png
--------------------------------------------------------------------------
"""
from __future__ import annotations

import html
import os
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

REPO_ROOT = Path(__file__).resolve().parent.parent
SCREENS_DIR = REPO_ROOT / "docs" / "screens"

KUBE_CONTEXT = os.environ.get("K8S_CAPTURE_CONTEXT", "kind-etri-edge")
NAMESPACE = "edge-auth"
DASHBOARD_NAMESPACE = "kubernetes-dashboard"
DASHBOARD_SA = "eam-admin"
DASHBOARD_LOCAL_PORT = int(os.environ.get("K8S_CAPTURE_DASHBOARD_PORT", "8443"))
VIEWPORT = {"width": 1500, "height": 1000}

# Terminal-style HTML shell so kubectl stdout renders exactly as captured
# (verbatim inside <pre>), matching the look of the other capture scripts'
# terminal screenshots in this deck.
PAGE_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><title>{title}</title>
<style>
  html,body {{ margin:0; padding:0; background:#0f1115; }}
  .win {{ width:{width}px; font-family: 'Cascadia Mono','Consolas',monospace; }}
  .bar {{ height:34px; background:#2d303a; display:flex; align-items:center; padding:0 14px; }}
  .dot {{ width:12px; height:12px; border-radius:50%; margin-right:8px; }}
  .r {{ background:#ff5f56; }} .y {{ background:#ffbd2e; }} .g {{ background:#27c93f; }}
  .bar span {{ color:#c8cdd6; font-size:14px; margin-left:12px; }}
  pre {{
    color:#dee2e6; background:#0f1115; margin:0; padding:20px;
    font-size:15px; line-height:1.55; white-space:pre; overflow-x:auto;
  }}
  .section {{ color:#7ee787; font-size:14px; padding:14px 20px 0 20px; margin:0; }}
</style></head>
<body>
<div class="win">
  <div class="bar">
    <div class="dot r"></div><div class="dot y"></div><div class="dot g"></div>
    <span>{title}</span>
  </div>
  {body}
</div>
</body></html>
"""

SECTION_TEMPLATE = '<p class="section">$ {cmd}</p><pre>{out}</pre>'


def run_kubectl(args: list[str]) -> str:
    """Run a real kubectl command against KUBE_CONTEXT and return its stdout."""
    cmd = ["kubectl", "--context", KUBE_CONTEXT, *args]
    result = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True,
                             encoding="utf-8", errors="replace")
    out = result.stdout or ""
    if result.returncode != 0:
        out += f"\n[stderr]\n{result.stderr}"
    return out.rstrip("\n")


def render_and_shot(page, title: str, sections: list[tuple[str, str]],
                     out_name: str, width: int = 1500) -> None:
    body = "\n".join(
        SECTION_TEMPLATE.format(cmd=html.escape(cmd), out=html.escape(out))
        for cmd, out in sections
    )
    html_doc = PAGE_TEMPLATE.format(title=html.escape(title), body=body, width=width)
    tmp_html = SCREENS_DIR / f"_tmp_{out_name}.html"
    tmp_html.write_text(html_doc, encoding="utf-8")
    page.goto(tmp_html.as_uri())
    page.wait_for_timeout(150)
    win = page.locator(".win")
    out_path = SCREENS_DIR / out_name
    win.screenshot(path=str(out_path))
    tmp_html.unlink(missing_ok=True)
    print(f"[k8s-capture] wrote {out_path.relative_to(REPO_ROOT)}")


def capture_pods_and_state(page) -> None:
    """k8s_pods.png: real cluster state -- nodes, pods, and secret/svc NAMES only."""
    sections = [
        ("kubectl get nodes -o wide",
         run_kubectl(["get", "nodes", "-o", "wide"])),
        (f"kubectl get pods -n {NAMESPACE} -o wide",
         run_kubectl(["get", "pods", "-n", NAMESPACE, "-o", "wide"])),
        (f"kubectl get secret,svc -n {NAMESPACE}",
         run_kubectl(["get", "secret,svc", "-n", NAMESPACE])),
    ]
    render_and_shot(page, "kubectl -- etri-edge cluster state", sections, "k8s_pods.png")


def capture_agent_log(page) -> None:
    """k8s_agent_log.png: real enroll -> token -> telemetry cycle from agent-001."""
    out = run_kubectl(["logs", "deploy/agent-001", "-n", NAMESPACE, "--tail=40"])
    sections = [(f"kubectl logs deploy/agent-001 -n {NAMESPACE} --tail=40", out)]
    render_and_shot(page, "kubectl logs -- agent-001 (edge-auth)", sections,
                     "k8s_agent_log.png")


def ensure_dashboard_port_forward() -> subprocess.Popen | None:
    """Start `kubectl port-forward` to the Dashboard unless already listening."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        if s.connect_ex(("127.0.0.1", DASHBOARD_LOCAL_PORT)) == 0:
            print(f"[k8s-capture] dashboard already reachable on :{DASHBOARD_LOCAL_PORT}")
            return None

    proc = subprocess.Popen(
        ["kubectl", "--context", KUBE_CONTEXT, "-n", DASHBOARD_NAMESPACE,
         "port-forward", "svc/kubernetes-dashboard",
         f"{DASHBOARD_LOCAL_PORT}:443"],
        cwd=REPO_ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(30):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", DASHBOARD_LOCAL_PORT)) == 0:
                print(f"[k8s-capture] started port-forward on :{DASHBOARD_LOCAL_PORT}")
                return proc
        time.sleep(0.5)
    proc.terminate()
    raise RuntimeError("Dashboard port-forward did not become reachable")


def capture_dashboard(page) -> None:
    """k8s_dashboard.png: real Kubernetes Dashboard, logged in, edge-auth workloads."""
    token = subprocess.run(
        ["kubectl", "--context", KUBE_CONTEXT, "-n", DASHBOARD_NAMESPACE,
         "create", "token", DASHBOARD_SA, "--duration=24h"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
        encoding="utf-8", errors="replace",
    ).stdout.strip()

    pf_proc = ensure_dashboard_port_forward()
    try:
        page.goto(f"https://127.0.0.1:{DASHBOARD_LOCAL_PORT}/",
                   wait_until="networkidle")
        page.wait_for_selector("textarea#token, input#token", timeout=20000)
        token_field = page.locator("textarea#token, input#token").first
        token_field.fill(token)
        page.locator("button[type='submit'], .kd-login-signin-button").first.click()
        page.wait_for_selector("text=Workloads", timeout=20000)

        # Navigate straight to the edge-auth namespace workloads overview.
        page.goto(
            f"https://127.0.0.1:{DASHBOARD_LOCAL_PORT}/#/workloads?namespace={NAMESPACE}",
            wait_until="networkidle",
        )
        page.wait_for_selector("text=Deployments", timeout=20000)
        page.wait_for_timeout(1500)
        out_path = SCREENS_DIR / "k8s_dashboard.png"
        page.screenshot(path=str(out_path), full_page=False)
        print(f"[k8s-capture] wrote {out_path.relative_to(REPO_ROOT)}")
    finally:
        if pf_proc is not None:
            pf_proc.terminate()


def main() -> None:
    SCREENS_DIR.mkdir(parents=True, exist_ok=True)

    check = subprocess.run(
        ["kubectl", "--context", KUBE_CONTEXT, "get", "ns", NAMESPACE],
        cwd=REPO_ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if check.returncode != 0:
        print(f"[k8s-capture] ERROR: context {KUBE_CONTEXT!r} / namespace "
              f"{NAMESPACE!r} not reachable. See this file's docstring for "
              "cluster setup steps.", file=sys.stderr)
        sys.exit(1)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=VIEWPORT, ignore_https_errors=True)

        capture_pods_and_state(page)
        capture_agent_log(page)
        try:
            capture_dashboard(page)
        except Exception as exc:  # noqa: BLE001 -- report, don't fail the whole run
            print(f"[k8s-capture] WARNING: dashboard capture failed: {exc}",
                  file=sys.stderr)

        browser.close()
    print("[k8s-capture] done")


if __name__ == "__main__":
    main()
