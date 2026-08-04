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
    print("[capture] done")


if __name__ == "__main__":
    main()
