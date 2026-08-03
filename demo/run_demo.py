"""demo/run_demo.py - 보안 적용 전/후 비교 로컬 시연 스크립트.

클러스터(K8s/KubeEdge) 없이 로컬에서 Manager를 두 번(비보안 -> 보안) 기동해
동일한 "미인증 텔레메트리 주입" 시도가 어떻게 달라지는지 보여준다.

  1단계 (before): INSECURE_MODE=true로 Manager를 기동한다. 등록/인증 절차를
     전혀 거치지 않은 공격자가 임의의 device_id로 텔레메트리를 그대로
     주입할 수 있음을 보여준다.
  2단계 (after): INSECURE_MODE=false로 Manager를 재기동한다. 동일한 시도는
     401로 거부되고, 정식으로 CSR 등록 -> 인증서 발급 -> bearer JWT 인증 ->
     JWS 서명을 거친 디바이스만 텔레메트리 전송에 성공한다.
  3단계: 2단계 Manager의 감사로그(audit log)를 tail로 출력해 거부/성공
     이벤트가 모두 기록되어 있음을 확인한다.

Windows 호환성 메모:
  - 서버는 ``sys.executable -m uvicorn ... --factory``로 기동한다(자식
    프로세스가 정확히 이 인터프리터/venv를 사용하도록).
  - 기동 완료는 고정 sleep이 아니라 ``/healthz`` 폴링으로 확인한다.
  - 종료는 ``proc.terminate()`` + ``wait()``을 우선 시도하고, 시간 내
    끝나지 않으면 ``taskkill /F /T``로 강제 종료한다(자식 프로세스까지
    확실히 정리해 좀비 uvicorn을 남기지 않기 위함).

``--fast``는 시연용 연출 sleep만 생략한다. 서버 기동 대기는 항상 폴링
방식이므로 --fast 여부와 무관하게 동작한다(CI/pytest에서 안전).
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import IO, List, Optional, Tuple

import httpx

from eam.agent.agent import EdgeAgent
from eam.common.audit import AuditLog

REPO_ROOT = Path(__file__).resolve().parent.parent

ATTACKER_DEVICE_ID = "attacker-drone-01"
LEGIT_DEVICE_ID = "legit-sensor-01"

_ACTIVE_PROCS: List[subprocess.Popen] = []


def _cleanup_all_procs() -> None:
    """프로세스 누락 방지용 안전망 - 정상 흐름에서는 이미 각 단계에서 정리된다."""
    for proc in list(_ACTIVE_PROCS):
        _stop_process(proc)


atexit.register(_cleanup_all_procs)


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _stop_process(proc: subprocess.Popen, timeout: float = 10.0) -> None:
    """자식 uvicorn 프로세스를 확실히 종료한다 (Windows: taskkill 폴백)."""
    if proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                    check=False,
                )
            else:
                proc.kill()
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                pass
    if proc in _ACTIVE_PROCS:
        _ACTIVE_PROCS.remove(proc)


def _narrate(msg: str, *, fast: bool, pause: float = 0.8) -> None:
    print(msg)
    if not fast:
        time.sleep(pause)


async def _wait_healthz(port: int, timeout: float = 40.0) -> None:
    deadline = time.monotonic() + timeout
    last_exc: Optional[BaseException] = None
    async with httpx.AsyncClient() as client:
        while time.monotonic() < deadline:
            try:
                resp = await client.get(f"http://127.0.0.1:{port}/healthz", timeout=2.0)
                if resp.status_code == 200:
                    return
            except Exception as exc:  # noqa: BLE001 - 폴링 중 일시 오류는 재시도
                last_exc = exc
            await asyncio.sleep(0.3)
    raise RuntimeError(f"Manager가 {timeout}s 내에 기동되지 않았습니다 (port={port}): {last_exc}")


def _launch_manager(
    *, port: int, work_dir: Path, insecure: bool, bootstrap_token: str
) -> Tuple[subprocess.Popen, IO[str]]:
    env = os.environ.copy()
    env["CERTS_DIR"] = str(work_dir / "certs")
    env["DB_URL"] = str(work_dir / "manager.db")
    env["INSECURE_MODE"] = "true" if insecure else "false"
    env["AUTO_APPROVE"] = "true"
    env["BOOTSTRAP_TOKEN"] = bootstrap_token

    log_file = open(work_dir / "manager_stdout.log", "w", encoding="utf-8")
    cmd = [
        sys.executable, "-m", "uvicorn",
        "eam.manager.app:create_app", "--factory",
        "--host", "127.0.0.1", "--port", str(port),
        "--log-level", "warning",
    ]
    popen_kwargs = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(
        cmd, cwd=str(REPO_ROOT), env=env, stdout=log_file, stderr=subprocess.STDOUT, **popen_kwargs
    )
    _ACTIVE_PROCS.append(proc)
    return proc, log_file


def _audit_db_path(db_url: Path) -> Path:
    """eam.manager.app.create_app()이 audit_db_path를 안 받았을 때 쓰는 것과
    동일한 파생 규칙(DB_URL stem + "_audit")을 재현한다."""
    return db_url.with_name(db_url.stem + "_audit" + (db_url.suffix or ".db"))


async def _phase_insecure(tmp_root: Path, *, fast: bool) -> None:
    _narrate("\n[1단계] 보안 적용 '전' - Manager를 INSECURE_MODE=true로 기동합니다.", fast=fast)
    work_dir = tmp_root / "phase1-insecure"
    work_dir.mkdir(parents=True, exist_ok=True)
    port = find_free_port()
    proc, log_file = _launch_manager(
        port=port, work_dir=work_dir, insecure=True, bootstrap_token=secrets.token_urlsafe(16)
    )
    try:
        await _wait_healthz(port)
        _narrate(f"  Manager 기동 완료 (127.0.0.1:{port}, INSECURE_MODE=true)", fast=fast)

        _narrate(
            f"  공격자 시나리오: 등록/인증 절차 없이 device_id={ATTACKER_DEVICE_ID!r}로 "
            "텔레메트리를 직접 주입합니다 (Bearer 토큰 없음, 서명 검증 없음).",
            fast=fast,
        )
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            resp = await client.post(
                "/api/v1/telemetry",
                json={"device_id": ATTACKER_DEVICE_ID, "jws": "not-a-real-signature"},
            )
        if resp.status_code != 200:
            raise RuntimeError(
                f"1단계에서 200이 예상되었으나 HTTP {resp.status_code} {resp.text}"
            )
        _narrate(
            f"  [결과] HTTP {resp.status_code} {resp.json()} -> "
            "미인증 디바이스의 텔레메트리 주입이 그대로 수용되었습니다 (BEFORE).",
            fast=fast,
        )
    finally:
        _stop_process(proc)
        log_file.close()


async def _phase_secure(tmp_root: Path, *, fast: bool) -> Path:
    _narrate("\n[2단계] 보안 적용 '후' - Manager를 INSECURE_MODE=false로 재기동합니다.", fast=fast)
    work_dir = tmp_root / "phase2-secure"
    work_dir.mkdir(parents=True, exist_ok=True)
    port = find_free_port()
    bootstrap_token = secrets.token_urlsafe(16)
    proc, log_file = _launch_manager(
        port=port, work_dir=work_dir, insecure=False, bootstrap_token=bootstrap_token
    )
    try:
        await _wait_healthz(port)
        _narrate(f"  Manager 기동 완료 (127.0.0.1:{port}, INSECURE_MODE=false)", fast=fast)

        _narrate("  동일한 미인증 주입 시도를 다시 재현합니다...", fast=fast)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            resp = await client.post(
                "/api/v1/telemetry",
                json={"device_id": ATTACKER_DEVICE_ID, "jws": "not-a-real-signature"},
            )
        if resp.status_code != 401:
            raise RuntimeError(
                f"2단계에서 401이 예상되었으나 HTTP {resp.status_code} {resp.text}"
            )
        _narrate(
            f"  [결과] HTTP {resp.status_code} -> 미인증 요청이 거부되었습니다 (AFTER).",
            fast=fast,
        )

        _narrate(
            f"  정식 디바이스 시나리오: device_id={LEGIT_DEVICE_ID!r}가 "
            "CSR 등록 -> 인증서 발급 -> bearer JWT 인증 -> JWS 서명 순으로 진행합니다.",
            fast=fast,
        )
        agent = EdgeAgent(
            device_id=LEGIT_DEVICE_ID,
            site="factory-A",
            group="sensors",
            manager_url=f"http://127.0.0.1:{port}",
            certs_dir=work_dir / "agent_certs",
        )
        try:
            status = await agent.enroll(bootstrap_token)
            _narrate(f"    - enroll() -> status={status!r}", fast=fast)
            token = await agent.get_token()
            _narrate(f"    - get_token() -> bearer JWT 발급됨 (길이={len(token)}자)", fast=fast)
            ok = await agent.send_telemetry(
                {"sensor_type": "temperature", "value": 23.5, "unit": "celsius", "ts": time.time()}
            )
            if not ok:
                raise RuntimeError("정식 디바이스의 텔레메트리 전송이 실패했습니다.")
            _narrate(
                "  [결과] 정식으로 등록/인증된 디바이스의 텔레메트리 전송에 성공했습니다 (AFTER).",
                fast=fast,
            )
        finally:
            await agent.aclose()
    finally:
        _stop_process(proc)
        log_file.close()

    return _audit_db_path(work_dir / "manager.db")


def _print_audit_tail(audit_path: Path, *, fast: bool) -> None:
    _narrate("\n[3단계] 보안 모드(2단계) 감사로그(audit log) tail", fast=fast)
    if not audit_path.exists():
        print("  (감사로그 파일을 찾을 수 없습니다)")
        return

    audit = AuditLog(audit_path)
    try:
        records = audit.query(limit=15)
    finally:
        audit.close()

    for rec in reversed(records):  # 시간순으로 보기 좋게 뒤집는다 (query()는 최신순).
        device = rec.device_id or "-"
        print(f"    [{rec.ts}] event={rec.event:<16} device={device:<20} outcome={rec.outcome:<8} {rec.detail}")


async def run_demo(*, fast: bool) -> int:
    print("=" * 70)
    print("EAM 보안 적용 전/후(Before/After) 비교 로컬 데모 (클러스터 불필요)")
    print("=" * 70)

    tmp_root = Path(tempfile.mkdtemp(prefix="eam-demo-"))
    try:
        await _phase_insecure(tmp_root, fast=fast)
        audit_path = await _phase_secure(tmp_root, fast=fast)
        _print_audit_tail(audit_path, fast=fast)
    except Exception as exc:  # noqa: BLE001 - 데모 스크립트 최상위: 실패 사유를 출력하고 exit 1
        print(f"\n[실패] {exc}")
        return 1
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    print("\n" + "=" * 70)
    print("데모 완료: INSECURE_MODE 해제만으로 미인증 텔레메트리 주입이 차단되고,")
    print("정식 등록/인증 절차를 거친 디바이스만 통신할 수 있음을 확인했습니다.")
    print("=" * 70)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="EAM 보안 적용 전/후 비교 로컬 데모")
    parser.add_argument(
        "--fast", action="store_true", help="연출용 대기(sleep)를 생략하고 빠르게 실행 (테스트/CI용)"
    )
    args = parser.parse_args(argv)
    return asyncio.run(run_demo(fast=args.fast))


if __name__ == "__main__":
    sys.exit(main())
