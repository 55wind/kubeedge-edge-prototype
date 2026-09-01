"""Capture the CLI-side platform screens from REAL command output.

Each screen here is produced by actually running the program, taking its
genuine stdout verbatim, and rendering it in a real Chromium browser (the same
renderer used by ``ppt/capture_k8s_screens.py``). Nothing is retyped or
simulated: if the program's output changes, the screenshot changes.

Covers:
  screen_demo.png     - demo/run_demo.py --fast      (보안 적용 전·후 비교, 수행항목 7)
  screen_gateway.png  - EdgeGateway 사설망 집선 실행  (수행항목 5)
  screen_fleet.png    - eam.simulator.fleet CLI       (수행항목 1·3)
  screen_attack.png   - security/attack_scenarios.py  (시나리오 기반 공격 테스트, 수행항목 3·6)

Run: python ppt/capture_cli_screens.py
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from capture_k8s_screens import SCREENS_DIR, VIEWPORT, render_and_shot  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

# Drives a real EdgeGateway against a real Manager and prints what happened.
# Kept as a module-level source string so the screenshot shows the exact
# command that produced it.
GATEWAY_SCRIPT = r"""
import asyncio, json, os, tempfile
os.environ["AUTO_APPROVE"] = "true"
import httpx
from eam.gateway.gateway import EdgeGateway
from eam.manager.app import create_app

async def main():
    tmp = tempfile.mkdtemp(prefix="eam-gw-")
    app = create_app(store_db_path=tmp + "/m.db", audit_db_path=tmp + "/a.db",
                     certs_dir=tmp + "/certs")
    tr = httpx.ASGITransport(app=app)
    gw = EdgeGateway("gateway-001", site="factory-B", group="gateway",
                     manager_url="http://manager", certs_dir=tmp + "/gw", transport=tr)
    status = await gw.enroll(app.state.bootstrap_token)
    print(f"[gateway-001] Manager 등록/인증서 발급 완료 (status={status}) - 공인 접점은 게이트웨이 1개뿐")
    for sub in ("priv-sensor-01", "priv-sensor-02", "priv-sensor-03"):
        gw.attach(sub)
        print(f"[gateway-001] 사설IP 하위 디바이스 attach: {sub} (Manager에 개별 등록되지 않음)")
    ok = await gw.send_batch_telemetry(["temperature"])
    print(f"[gateway-001] 하위 디바이스 센서값을 JWS 서명 배치 1건으로 업링크 전송 -> 성공={ok}")
    print()
    print("=== Manager 측 저장 결과 (실제 DB 조회) ===")
    for r in app.state.store.list_telemetry(device_id="gateway-001"):
        p = json.loads(r.payload_json)
        print(f"telemetry id={r.id} device_id={r.device_id} verified={bool(r.verified)}")
        print(f"  gateway_id={p['gateway_id']} batch={len(p['batch'])}건:")
        for b in p["batch"]:
            rest = ", ".join(f"{k}={v}" for k, v in b.items() if k != "device_id")
            print(f"    - {b['device_id']}: {rest}")
    for sub in ("priv-sensor-01", "priv-sensor-02", "priv-sensor-03"):
        print(f"devices 테이블 조회 {sub}: {app.state.store.get_device(sub)}  (사설 디바이스는 미노출)")
    await gw.aclose()

asyncio.run(main())
"""


def run(cmd: list[str], *, stdin_text: str | None = None, timeout: int = 900) -> str:
    """Run a real command and return its genuine stdout (stderr appended on failure)."""
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    proc = subprocess.run(cmd, cwd=REPO_ROOT, env=env, input=stdin_text,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)
    out = proc.stdout or ""
    if proc.returncode != 0:
        out += f"\n[exit {proc.returncode}]\n{proc.stderr}"
    return out.rstrip("\n")


def main() -> None:
    SCREENS_DIR.mkdir(parents=True, exist_ok=True)
    demo_out = run([sys.executable, "demo/run_demo.py", "--fast"])
    gateway_out = run([sys.executable, "-c", GATEWAY_SCRIPT])
    fleet_out = run([sys.executable, "-m", "eam.simulator.fleet",
                     "--n", "20", "--concurrency", "8"])
    attack_out = run([sys.executable, "security/attack_scenarios.py"])

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=VIEWPORT)
        render_and_shot(
            page, "보안 적용 전·후 비교 시연 — demo/run_demo.py 실행 화면",
            [("python demo/run_demo.py --fast", demo_out)], "screen_demo.png",
        )
        render_and_shot(
            page, "게이트웨이+사설IP 집선 — EdgeGateway 실제 실행 (수행항목 5)",
            [("python -m eam.gateway  (사설망 게이트웨이 집선 시나리오)", gateway_out)],
            "screen_gateway.png",
        )
        render_and_shot(
            page, "가상 디바이스 20기 일괄 등록·인증·전송 — 시뮬레이터 실행 화면",
            [("python -m eam.simulator.fleet --n 20 --concurrency 8", fleet_out)],
            "screen_fleet.png",
        )
        render_and_shot(
            page, "시나리오 기반 공격 테스트 — 실제 Manager 대상 red-team 15종 (수행항목 3·6)",
            [("python security/attack_scenarios.py", attack_out)],
            "screen_attack.png",
        )
        browser.close()
    print("[cli-capture] done")


if __name__ == "__main__":
    main()
