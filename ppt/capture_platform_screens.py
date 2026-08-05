"""Capture REAL platform screens: a live Manager driven through Swagger UI.

Unlike ``ppt/capture_screens.py`` (which renders command output as images),
this script boots the actual Manager with uvicorn, opens its Swagger UI in a
real Chromium browser (Playwright), and *executes real requests* through the
UI — the screenshots therefore show genuine platform pages with genuine
responses (issued X.509 certificate, JWT, device list, audit trail).

Run:  python ppt/capture_platform_screens.py
Output: docs/screens/platform_*.png

Demo credentials are generated per run and injected via environment variables;
nothing secret is committed. The operator password shown in the request pane is
an ephemeral, capture-only value for a server that no longer exists afterwards.
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

REPO_ROOT = Path(__file__).resolve().parent.parent
SCREENS_DIR = REPO_ROOT / "docs" / "screens"
HOST, PORT = "127.0.0.1", 18500
BASE = f"http://{HOST}:{PORT}"

DEMO_OPERATOR_USER = "operator"
DEMO_OPERATOR_PASS = "demo-only-password"   # capture-only, ephemeral server
VIEWPORT = {"width": 1500, "height": 1000}


def seed_state(tmp: Path) -> dict:
    """Drive real traffic into a fresh Manager state so the UI has data to show."""
    import asyncio

    from eam.agent.agent import EdgeAgent, PermanentTelemetryError
    from eam.common import jws as jws_mod
    from eam.common import pki
    from eam.manager.app import create_app

    bootstrap = secrets.token_urlsafe(24)
    os.environ.update(
        AUTO_APPROVE="true",
        BOOTSTRAP_TOKEN=bootstrap,
        EAM_OPERATOR_USERNAME=DEMO_OPERATOR_USER,
        EAM_OPERATOR_PASSWORD=DEMO_OPERATOR_PASS,
        EAM_ADMIN_USERNAME="admin",
        EAM_ADMIN_PASSWORD=DEMO_OPERATOR_PASS,
    )

    async def _seed():
        app = create_app(store_db_path=tmp / "m.db", audit_db_path=tmp / "a.db",
                         certs_dir=tmp / "certs")
        transport = httpx.ASGITransport(app=app)

        async def device(did, site, group):
            a = EdgeAgent(did, site=site, group=group, manager_url="http://m",
                          certs_dir=tmp / did, transport=transport)
            await a.enroll(bootstrap)
            return a

        d1 = await device("sensor-jetson-01", "factory-A", "sensors")
        await d1.send_telemetry({"sensor_type": "temperature", "value": 23.4})
        d2 = await device("sensor-rpi-02", "factory-B", "sensors")
        await d2.send_telemetry({"sensor_type": "humidity", "value": 41.2})
        d3 = await device("sensor-rpi-03", "factory-B", "sensors")

        async with httpx.AsyncClient(transport=transport, base_url="http://m") as c:
            # A real forged-signature attempt: valid JWT, payload signed by another key.
            token = await d3.get_token()
            _, wrong_key = pki.create_csr("attacker")
            forged = jws_mod.sign_payload({"sensor_type": "temperature", "value": -99},
                                          wrong_key)
            await c.post("/api/v1/telemetry",
                         json={"device_id": "sensor-rpi-03", "jws": forged},
                         headers={"Authorization": f"Bearer {token}"})
            r = await c.post("/api/v1/auth/operator",
                             json={"username": "admin", "password": DEMO_OPERATOR_PASS})
            hdr = {"Authorization": f"Bearer {r.json()['access_token']}"}
            await c.post("/api/v1/devices/sensor-rpi-03/revoke", headers=hdr)
            try:
                d3._token = None
                await d3.send_telemetry({"sensor_type": "temperature", "value": 20.0})
            except PermanentTelemetryError:
                pass

        for a in (d1, d2, d3):
            await a.aclose()

    asyncio.run(_seed())
    return {"bootstrap": bootstrap}


def start_server(tmp: Path) -> subprocess.Popen:
    env = dict(os.environ, PYTHONIOENCODING="utf-8", CERTS_DIR=str(tmp / "certs"))
    proc = subprocess.Popen(
        [sys.executable, "-c",
         "import uvicorn;from eam.manager.app import create_app;"
         f"uvicorn.run(create_app(store_db_path=r'{tmp / 'm.db'}',"
         f"audit_db_path=r'{tmp / 'a.db'}',certs_dir=r'{tmp / 'certs'}'),"
         f"host='{HOST}',port={PORT},log_level='warning')"],
        cwd=REPO_ROOT, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(60):
        try:
            if httpx.get(f"{BASE}/healthz", timeout=1).status_code == 200:
                return proc
        except Exception:
            time.sleep(0.5)
    proc.terminate()
    raise RuntimeError("Manager did not become healthy")


# --- Swagger UI helpers -----------------------------------------------------

# Swagger UI appends the static response-schema documentation under every
# executed call; for slide captures only the real request/response matters, so
# drop everything that follows the live response block before screenshotting.
TRIM_JS = """
() => {
  document.querySelectorAll('.responses-inner').forEach((inner) => {
    const live = inner.querySelector('.live-responses-table');
    if (!live) return;
    let node = live;
    while (node.parentElement && node.parentElement !== inner) node = node.parentElement;
    let sib = node.nextElementSibling;
    while (sib) { const next = sib.nextElementSibling; sib.remove(); sib = next; }
  });
}
"""


def opblock(page, path_text: str, method: str):
    return page.locator(f'.opblock-{method}', has=page.locator(
        '.opblock-summary-path', has_text=path_text)).first


def authorize_with(page, token: str) -> None:
    """Set (or replace) the bearer token in Swagger UI's Authorize dialog."""
    page.locator("button.authorize").first.click()
    logout = page.locator(".auth-btn-wrapper button.btn-done").locator(
        "xpath=preceding-sibling::button[normalize-space()='Logout']")
    if logout.count():
        logout.first.click()
    page.locator(".auth-container input").first.fill(token)
    page.locator(".auth-btn-wrapper button.authorize").click()
    page.locator(".auth-btn-wrapper button.btn-done").click()
    page.wait_for_timeout(300)


def operator_token(page, username: str, capture_as: str | None = None) -> str:
    """Log in through the real /auth/operator endpoint and return the issued JWT."""
    block = execute_operation(
        page, "/api/v1/auth/operator", "post",
        json.dumps({"username": username, "password": DEMO_OPERATOR_PASS}, indent=2),
    )
    if capture_as:
        shot(block, capture_as)
    token = json.loads(
        block.locator(".live-responses-table .microlight").first.inner_text()
    )["access_token"]
    collapse_all(page)
    return token


def execute_operation(page, path_text: str, method: str, body: str | None = None):
    """Expand an operation, fill the request body, press Execute, wait for a response."""
    block = opblock(page, path_text, method)
    block.locator(".opblock-summary").click()
    block.locator(".opblock-body").wait_for(timeout=15000)
    # Re-executing an operation keeps it in try-out mode (the button becomes
    # Cancel/Reset), so only click the plain "Try it out" button when present.
    try:
        block.locator("button.try-out__btn:not(.cancel):not(.reset)").first.click(
            timeout=4000
        )
    except PlaywrightTimeout:
        pass  # already in try-out mode from a previous execution
    if body is not None:
        ta = block.locator("textarea.body-param__text")
        ta.fill(body)
    block.locator("button.execute").click()
    block.locator(".live-responses-table").wait_for(timeout=20000)
    page.wait_for_timeout(400)
    return block


def shot(block, out_name: str) -> None:
    block.page.evaluate(TRIM_JS)
    block.page.wait_for_timeout(150)
    path = SCREENS_DIR / out_name
    block.screenshot(path=str(path))
    print(f"[platform] wrote {path.relative_to(REPO_ROOT)}")


def collapse_all(page):
    for b in page.locator(".opblock.is-open .opblock-summary").all():
        b.click()
    page.wait_for_timeout(200)


def main() -> None:
    SCREENS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="eam-platform-"))
    info = seed_state(tmp)
    proc = start_server(tmp)
    try:
        from eam.common import pki

        csr_pem, _ = pki.create_csr("sensor-new-04")
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport=VIEWPORT)
            page.goto(f"{BASE}/docs", wait_until="networkidle")
            page.wait_for_selector(".swagger-ui .opblock", timeout=20000)

            # 1) API 계약 전체 (수행항목 6)
            page.screenshot(path=str(SCREENS_DIR / "platform_swagger_overview.png"))
            print("[platform] wrote docs/screens/platform_swagger_overview.png")

            # 2) 실제 디바이스 등록 -> X.509 발급 (수행항목 3)
            block = execute_operation(
                page, "/api/v1/devices/register", "post",
                json.dumps({"device_id": "sensor-new-04", "site": "factory-A",
                            "group": "sensors", "csr_pem": csr_pem.decode(),
                            "bootstrap_token": info["bootstrap"]}, indent=2),
            )
            shot(block, "platform_register.png")
            collapse_all(page)

            # 3) 운영자 인증 -> 실제 JWT 발급, 그 토큰으로 Authorize (수행항목 3·6)
            token = operator_token(page, DEMO_OPERATOR_USER,
                                   capture_as="platform_auth_operator.png")
            authorize_with(page, token)

            # 4) 인증된 상태에서 디바이스 현황 (revoked 포함)
            block = execute_operation(page, "/api/v1/devices", "get")
            shot(block, "platform_devices.png")
            collapse_all(page)

            # 5) operator 역할로 admin 전용 API 호출 -> 실제 403 (RBAC 인가 실증)
            block = execute_operation(page, "/api/v1/audit", "get")
            shot(block, "platform_rbac_denied.png")
            collapse_all(page)

            # 6) admin 역할로 재인증 후 동일 API -> 200, 감사 실기록 조회
            admin_token = operator_token(page, "admin")
            authorize_with(page, admin_token)
            block = execute_operation(page, "/api/v1/audit", "get")
            shot(block, "platform_audit.png")

            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    print("[platform] done")


if __name__ == "__main__":
    main()
