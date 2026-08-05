"""Render the two document/manifest comparison screens for the mapping deck.

These two images are the only deck visuals that are *not* screenshots of a
running system, because what they show is a document and a source-file
comparison rather than a live UI:

  screen_comm_decision.png - docs/01 통신 방식 비교·선정 요약 (수행항목 2)
  screen_k8s_yaml.png      - 1차년도 vs 2차년도 배포 매니페스트 대조 (수행항목 4)

Every other deck screen is captured from the real platform:
  ppt/capture_platform_screens.py - live Manager driven through Swagger UI
  ppt/capture_k8s_screens.py      - real Kubernetes cluster + Dashboard
  ppt/capture_cli_screens.py      - real CLI stdout (demo / gateway / fleet)

Run: python ppt/capture_screens.py
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = Path(__file__).resolve().parent.parent
SCREENS_DIR = REPO_ROOT / "docs" / "screens"
FONT_PATH = r"C:\Windows\Fonts\malgun.ttf"
FONT_BOLD_PATH = r"C:\Windows\Fonts\malgunbd.ttf"

BG = (24, 26, 32)
TITLEBAR = (45, 48, 58)
FG = (222, 226, 230)
TITLE_FG = (200, 205, 214)
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


def main() -> None:
    SCREENS_DIR.mkdir(parents=True, exist_ok=True)
    capture_comm_decision()
    capture_k8s_yaml_diff()
    print("[capture] done")


if __name__ == "__main__":
    main()
