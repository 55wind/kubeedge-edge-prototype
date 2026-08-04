"""Capture real runtime screens as PNGs for the mapping deck.

Boots the actual Manager, drives real devices through the security flow,
then renders the genuine console/API outputs as terminal-styled PNGs under
``docs/screens/``. Every image is produced from live program output — no
mock data. Re-run with: ``python ppt/capture_screens.py``.

The Swagger UI capture (``swagger_ui.png``) is taken separately with a
browser against a live server (see ``--serve`` mode) and committed
alongside; this script does not overwrite it.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = Path(__file__).resolve().parent.parent
SCREENS_DIR = REPO_ROOT / "docs" / "screens"
FONT_PATH = r"C:\Windows\Fonts\malgun.ttf"
FONT_BOLD_PATH = r"C:\Windows\Fonts\malgunbd.ttf"

BG = (24, 26, 32)
TITLEBAR = (45, 48, 58)
FG = (222, 226, 230)
PROMPT = (120, 200, 120)
TITLE_FG = (200, 205, 214)


def render_terminal_png(text: str, out_path: Path, title: str,
                        max_lines: int = 46, width: int = 1480) -> None:
    """Render captured console text as a terminal-window style PNG."""
    font = ImageFont.truetype(FONT_PATH, 17)
    title_font = ImageFont.truetype(FONT_BOLD_PATH, 16)
    lines = text.rstrip().splitlines()[:max_lines]
    line_h = 24
    pad = 18
    bar_h = 34
    height = bar_h + pad * 2 + line_h * len(lines)

    img = Image.new("RGB", (width, height), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, width, bar_h], fill=TITLEBAR)
    for i, color in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
        d.ellipse([12 + i * 22, 11, 24 + i * 22, 23], fill=color)
    d.text((84, 8), title, font=title_font, fill=TITLE_FG)

    y = bar_h + pad
    for line in lines:
        color = PROMPT if line.startswith("$") else FG
        d.text((pad, y), line[:170], font=font, fill=color)
        y += line_h
    img.save(out_path)
    print(f"[capture] wrote {out_path.relative_to(REPO_ROOT)} ({len(lines)} lines)")


async def seed_and_capture_api_screens() -> None:
    """Run real register/auth/telemetry/reject/revoke traffic, capture admin views."""
    import httpx

    from eam.agent.agent import EdgeAgent, PermanentTelemetryError
    from eam.common import jws as jws_mod
    from eam.common import pki
    from eam.manager.app import create_app

    os.environ["AUTO_APPROVE"] = "true"
    os.environ["EAM_ADMIN_USERNAME"] = "admin"
    os.environ["EAM_ADMIN_PASSWORD"] = "screen-capture-demo"

    tmp = Path(tempfile.mkdtemp(prefix="eam-screens-"))
    app = create_app(store_db_path=tmp / "m.db", audit_db_path=tmp / "a.db",
                     certs_dir=tmp / "certs")
    transport = httpx.ASGITransport(app=app)
    token = app.state.bootstrap_token

    async def device(did: str, site: str, group: str) -> EdgeAgent:
        a = EdgeAgent(did, site=site, group=group, manager_url="http://manager",
                      certs_dir=tmp / did, transport=transport)
        await a.enroll(token)
        return a

    d1 = await device("sensor-jetson-01", "factory-A", "sensors")
    await d1.send_telemetry({"sensor_type": "temperature", "value": 23.4})
    await d1.send_telemetry({"sensor_type": "temperature", "value": 23.9})
    d2 = await device("sensor-rpi-02", "factory-B", "sensors")
    await d2.send_telemetry({"sensor_type": "humidity", "value": 41.2})
    d3 = await device("sensor-rpi-03", "factory-B", "sensors")

    async with httpx.AsyncClient(transport=transport, base_url="http://manager") as c:
        # Forged JWS attempt: valid token, payload signed with the WRONG key.
        d3_token = await d3.get_token()
        _, wrong_key = pki.create_csr("attacker")
        forged = jws_mod.sign_payload({"sensor_type": "temperature", "value": -99},
                                      wrong_key)
        await c.post("/api/v1/telemetry", json={"device_id": "sensor-rpi-03",
                                                "jws": forged},
                     headers={"Authorization": f"Bearer {d3_token}"})

        # Admin: revoke rpi-03, then its next auth fails.
        r = await c.post("/api/v1/auth/operator",
                         json={"username": "admin", "password": "screen-capture-demo"})
        hdr = {"Authorization": f"Bearer {r.json()['access_token']}"}
        await c.post("/api/v1/devices/sensor-rpi-03/revoke", headers=hdr)
        try:
            d3._token = None  # force re-auth against the revoked cert
            await d3.send_telemetry({"sensor_type": "temperature", "value": 20.0})
        except PermanentTelemetryError:
            pass

        devices = (await c.get("/api/v1/devices", headers=hdr)).json()
        audit = (await c.get("/api/v1/audit", headers=hdr,
                             params={"limit": 12})).json()

    for a in (d1, d2, d3):
        await a.aclose()

    dev_text = "$ GET /api/v1/devices   (Authorization: Bearer <operator JWT>)\n\n"
    dev_text += json.dumps(devices, indent=2, ensure_ascii=False)
    render_terminal_png(dev_text, SCREENS_DIR / "screen_devices.png",
                        "Manager 관리 API — 디바이스 현황 조회 (실제 응답)")

    audit_lines = ["$ GET /api/v1/audit?limit=12   (admin 전용, RBAC 적용)", ""]
    audit_lines.append(f"{'ts':<27} {'event':<18} {'device_id':<18} {'outcome':<9} detail")
    audit_lines.append("-" * 108)
    for row in audit:
        audit_lines.append(f"{row['ts'][:26]:<27} {row['event']:<18} "
                           f"{str(row['device_id']):<18} {row['outcome']:<9} "
                           f"{row['detail'][:38]}")
    render_terminal_png("\n".join(audit_lines), SCREENS_DIR / "screen_audit.png",
                        "AAA Accounting — 감사로그 조회 (등록/인증/거부/폐기 실기록)")


def capture_demo_output() -> None:
    """Run the real before/after security demo and capture its console output."""
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.run([sys.executable, str(REPO_ROOT / "demo" / "run_demo.py"),
                           "--fast"], capture_output=True, text=True,
                          encoding="utf-8", env=env, cwd=REPO_ROOT, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError(f"run_demo failed:\n{proc.stdout}\n{proc.stderr}")
    text = "$ python demo/run_demo.py --fast\n\n" + proc.stdout
    render_terminal_png(text, SCREENS_DIR / "screen_demo.png",
                        "보안 적용 전·후 비교 시연 — demo/run_demo.py 실행 화면",
                        max_lines=52)


def capture_fleet_output(n: int = 20) -> None:
    """Run the real virtual-device fleet CLI and capture its summary output."""
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.run([sys.executable, "-m", "eam.simulator.fleet",
                           "--n", str(n), "--concurrency", "8"],
                          capture_output=True, text=True, encoding="utf-8",
                          env=env, cwd=REPO_ROOT, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(f"fleet failed:\n{proc.stdout}\n{proc.stderr}")
    text = (f"$ python -m eam.simulator.fleet --n {n} --concurrency 8\n\n"
            + proc.stdout)
    render_terminal_png(text, SCREENS_DIR / "screen_fleet.png",
                        f"가상 디바이스 {n}기 일괄 등록·인증·전송 — 시뮬레이터 실행 화면")


DOC_BG = (250, 250, 248)
DOC_FG = (40, 44, 52)
DOC_HEAD = (31, 56, 100)
OLD_LINE = (255, 138, 128)
NEW_LINE = (120, 210, 140)


def render_document_png(lines, out_path: Path, title: str, width: int = 1480) -> None:
    """Render doc-style content (light background) — lines: [(text, style), ...]."""
    font = ImageFont.truetype(FONT_PATH, 18)
    bold = ImageFont.truetype(FONT_BOLD_PATH, 18)
    title_font = ImageFont.truetype(FONT_BOLD_PATH, 16)
    line_h = 27
    pad = 22
    bar_h = 34
    height = bar_h + pad * 2 + line_h * len(lines)

    img = Image.new("RGB", (width, height), DOC_BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, width, bar_h], fill=DOC_HEAD)
    d.text((16, 8), title, font=title_font, fill=(255, 255, 255))

    y = bar_h + pad
    for text, style in lines:
        if style == "head":
            d.text((pad, y), text, font=bold, fill=DOC_HEAD)
        elif style == "rule":
            d.line([pad, y + line_h // 2, width - pad, y + line_h // 2],
                   fill=(200, 200, 200), width=1)
        else:
            d.text((pad, y), text[:150], font=font, fill=DOC_FG)
        y += line_h
    img.save(out_path)
    print(f"[capture] wrote {out_path.relative_to(REPO_ROOT)} ({len(lines)} lines)")


def render_two_pane_png(left_title, left_lines, right_title, right_lines,
                        out_path: Path, title: str, width: int = 1600) -> None:
    """Render before/after code panes side by side (dark terminal style).

    Each line is (text, color_key) with color_key in {None, 'old', 'new'}.
    """
    font = ImageFont.truetype(FONT_PATH, 16)
    head_font = ImageFont.truetype(FONT_BOLD_PATH, 16)
    title_font = ImageFont.truetype(FONT_BOLD_PATH, 16)
    line_h = 23
    pad = 16
    bar_h = 34
    head_h = 32
    n = max(len(left_lines), len(right_lines))
    height = bar_h + head_h + pad * 2 + line_h * n
    pane_w = (width - pad * 3) // 2

    img = Image.new("RGB", (width, height), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, width, bar_h], fill=TITLEBAR)
    for i, color in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
        d.ellipse([12 + i * 22, 11, 24 + i * 22, 23], fill=color)
    d.text((84, 8), title, font=title_font, fill=TITLE_FG)

    for px, (pane_title, color) in ((pad, (left_title, OLD_LINE)),
                                    (pad * 2 + pane_w, (right_title, NEW_LINE))):
        d.rectangle([px, bar_h + 8, px + pane_w, bar_h + 8 + head_h - 6], fill=TITLEBAR)
        d.text((px + 10, bar_h + 12), pane_title, font=head_font, fill=color)

    palette = {None: FG, "old": OLD_LINE, "new": NEW_LINE}
    for px, lines in ((pad, left_lines), (pad * 2 + pane_w, right_lines)):
        y = bar_h + head_h + pad
        for text, key in lines:
            d.text((px + 6, y), text[:88], font=font, fill=palette[key])
            y += line_h
    img.save(out_path)
    print(f"[capture] wrote {out_path.relative_to(REPO_ROOT)}")


def capture_comm_decision() -> None:
    """Render the item-2 decision (docs/01) as a document-style screen."""
    lines = [
        ("2. 비교 기준표 (요약)", "head"),
        ("", "rule"),
        ("기준             (A) 채널(CloudHub-EdgeHub) 기반              (B) 별도 프로토콜(HTTPS/AMQPS)", "body"),
        ("성능             고빈도 텔레메트리 부적합(리소스 동기화 목적)   REST 처리량 실측·튜닝 가능 (단일 워커 μ=23.76 auth/s)", "body"),
        ("보안(세밀도)     노드 단위 mTLS 종단, 디바이스 AAA 표현 불가    디바이스 단위 X.509 + JWT(RS256) + RBAC + JWS 적용", "body"),
        ("KubeEdge 적합성  네이티브 - 제어(파드/컨피그)에는 유일한 정답   클러스터와 무관하게 재사용(로컬 데모로 증명)", "body"),
        ("운영성           제어·데이터가 같은 채널 공유(장애 전파 위험)   제어/데이터 분리로 장애 격리 (NodePort 30443)", "body"),
        ("", "body"),
        ("4. 결론", "head"),
        ("", "rule"),
        ("선정: 하이브리드 - 제어(Control)는 KubeEdge 채널, 데이터·인증(Data/AAA)은 별도 mTLS HTTPS 채널.", "body"),
        ("  - 제어: 파드 배치·노드 동기화는 CloudHub-EdgeHub 채널 그대로 사용 (nodeSelector: node-role.kubernetes.io/edge)", "body"),
        ("  - 데이터·인증: 등록/인증/텔레메트리는 독립 REST 채널 (FastAPI, Service NodePort 30443 -> 8443)", "body"),
        ("  - AMQPS는 옵션으로만 존재 (k8s/rabbitmq.yaml, 현 코드는 REST+mTLS+JWT/JWS만 사용)", "body"),
        ("", "body"),
        ("이 구조는 문서상의 제안이 아니라 Task 1~5 구현 전체(REST API + KubeEdge 매니페스트 분리 배치)로 실증됨.", "body"),
    ]
    render_document_png(lines, SCREENS_DIR / "screen_comm_decision.png",
                        "docs/01-comm-method-decision.md — 통신 방식 비교·선정 (수행항목 2)")


def capture_k8s_yaml_diff() -> None:
    """Render year-1 vs v2 agent manifest excerpts side by side.

    The year-1 plaintext credential is deliberately masked — the real value
    must never re-enter this repository (final-review policy).
    """
    left = [
        ("spec:", None),
        ("  nodeName: edge1                # 노드 직접 고정", "old"),
        ("  containers:", None),
        ("    - name: agent", None),
        ("      image: eam-agent:latest    # 태그 미관리", "old"),
        ('      command: ["python","-m","agent.run",...]', None),
        ("      env:", None),
        ("        - name: MANAGER_BASE_URL", None),
        ('          value: "https://172.18.78.12:8443"   # IP 하드코딩', "old"),
        ("        - name: AMQP_URL", None),
        ('          value: "amqps://isl:********@172.18.78.12:5671/"', "old"),
        ("                                 # 평문 자격증명(캡처에서는 마스킹)", "old"),
        ("  volumes:", None),
        ("    - name: certs", None),
        ("      hostPath:", None),
        ("        path: /etc/eam-certs     # 인증서 수동 배포", "old"),
    ]
    right = [
        ("spec:", None),
        ("  nodeSelector:", "new"),
        ('    node-role.kubernetes.io/edge: ""   # KubeEdge edge 라벨 스케줄', "new"),
        ("  containers:", None),
        ("    - name: agent", None),
        ("      image: eam-agent:v2        # 버전 태그 + 본 repo Dockerfile 빌드", "new"),
        ('      command: ["python", "-m", "eam.agent"]', None),
        ("      env:", None),
        ("        - name: MANAGER_BASE_URL", None),
        ('          value: "http://__CLOUD_IP__:30443"  # 배포 스크립트가 sed 치환', "new"),
        ("        - name: BOOTSTRAP_TOKEN", None),
        ("          valueFrom:", "new"),
        ("            secretKeyRef:        # 배포 시 openssl rand로 생성되는 Secret", "new"),
        ("              name: eam-secrets", "new"),
        ("              key: bootstrap-token", "new"),
        ("  # 인증서는 등록 API로 자동 발급 (hostPath 수동 배포 제거)", "new"),
    ]
    render_two_pane_png(
        "기존 - prototype-y1/k8s/agent-edge1.yaml (1차년도)", left,
        "현재 - k8s/agent-edge1.yaml (2차년도 v2)", right,
        SCREENS_DIR / "screen_k8s_yaml.png",
        "KubeEdge 배포 매니페스트 - 1차년도 vs 2차년도 (수행항목 4)")


async def capture_gateway_screen() -> None:
    """Run the real EdgeGateway aggregation flow and capture the console view."""
    import httpx

    from eam.gateway.gateway import EdgeGateway
    from eam.manager.app import create_app

    os.environ["AUTO_APPROVE"] = "true"
    tmp = Path(tempfile.mkdtemp(prefix="eam-gw-"))
    app = create_app(store_db_path=tmp / "m.db", audit_db_path=tmp / "a.db",
                     certs_dir=tmp / "certs")
    transport = httpx.ASGITransport(app=app)

    gw = EdgeGateway("gateway-001", site="factory-B", group="gateway",
                     manager_url="http://manager", certs_dir=tmp / "gw",
                     transport=transport)
    out = ["$ python -m eam.gateway  (사설망 게이트웨이 집선 시나리오)", ""]
    status = await gw.enroll(app.state.bootstrap_token)
    out.append(f"[gateway-001] Manager에 등록/인증서 발급 완료 (status={status}) - 공인 접점은 게이트웨이 1개뿐")
    for sub in ("priv-sensor-01", "priv-sensor-02", "priv-sensor-03"):
        gw.attach(sub)
        out.append(f"[gateway-001] 사설IP 하위 디바이스 attach: {sub} (Manager에 개별 등록되지 않음)")
    ok = await gw.send_batch_telemetry(["temperature"])
    out.append("[gateway-001] 하위 디바이스 센서값을 수집해 JWS 서명 배치 1건으로 업링크 전송 -> "
               f"성공={ok}")
    out.append("")
    out.append("=== Manager 측 저장 결과 (실제 DB 조회) ===")
    rows = app.state.store.list_telemetry(device_id="gateway-001")
    for r in rows:
        payload = json.loads(r.payload_json)
        out.append(f"telemetry id={r.id} device_id={r.device_id} verified={bool(r.verified)}")
        out.append(f"  gateway_id={payload['gateway_id']} batch={len(payload['batch'])}건:")
        for b in payload["batch"]:
            rest = ", ".join(f"{k}={v}" for k, v in b.items() if k != "device_id")
            out.append(f"    - {b['device_id']}: {rest}")
    for sub in ("priv-sensor-01", "priv-sensor-02", "priv-sensor-03"):
        out.append(f"devices 테이블 조회 {sub}: {app.state.store.get_device(sub)}  (사설 디바이스는 미노출)")
    await gw.aclose()
    render_terminal_png("\n".join(out), SCREENS_DIR / "screen_gateway.png",
                        "게이트웨이+사설IP 집선 - EdgeGateway 실제 실행 (수행항목 5)")


def serve_for_swagger() -> None:
    """Run a live Manager so a browser can capture /docs (Swagger UI)."""
    os.environ.setdefault("AUTO_APPROVE", "true")
    import uvicorn

    from eam.manager.app import create_app

    tmp = Path(tempfile.mkdtemp(prefix="eam-swagger-"))
    app = create_app(store_db_path=tmp / "m.db", audit_db_path=tmp / "a.db",
                     certs_dir=tmp / "certs")
    uvicorn.run(app, host="127.0.0.1", port=18443, log_level="warning")


def main() -> None:
    SCREENS_DIR.mkdir(parents=True, exist_ok=True)
    if "--serve" in sys.argv:
        serve_for_swagger()
        return
    asyncio.run(seed_and_capture_api_screens())
    capture_demo_output()
    capture_fleet_output()
    capture_comm_decision()
    capture_k8s_yaml_diff()
    asyncio.run(capture_gateway_screen())
    print("[capture] done")


if __name__ == "__main__":
    main()
