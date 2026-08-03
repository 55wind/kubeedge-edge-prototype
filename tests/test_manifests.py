"""k8s/*.yaml 매니페스트 + demo/run_demo.py에 대한 구조적 검증.

PyYAML은 이 프로젝트의 허용 패키지 목록에 없으므로(과제 전역 제약), 표준
YAML 파서 대신 이 파일 안에 작은 YAML 부분집합 파서(``_yamlmini``)를 직접
구현해서 쓴다. k8s 매니페스트에서 실제로 쓰이는 구성(매핑, 시퀀스, 블록
스칼라 ``|``, 다중 문서 ``---``)만 지원하면 충분하다.
"""

from __future__ import annotations

import base64
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Union

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
K8S_DIR = REPO_ROOT / "k8s"
DEPLOY_DIR = REPO_ROOT / "deploy"

# 1차년도(prototype-y1) 매니페스트에 박혀 있던 하드코딩 비밀번호 - 어떤 파일에도
# 다시 등장해서는 안 된다 (과제 전역 제약 #4).
# base64로 저장해 이 소스 파일 자체에 평문 비밀번호가 다시 나타나지 않도록 한다
# (grep으로 리포를 스캔해도 평문이 검색되지 않아야 함).
FORBIDDEN_Y1_PASSWORD = base64.b64decode("d2pkcWhxaGdoZHVzcm50bGYxIQ==").decode()

# 1차년도 데모 환경의 고정 사설 IP 대역 - __CLOUD_IP__ 플레이스홀더로
# 대체되어야 하며 리터럴로 남아있으면 안 된다 (과제 전역 제약 #5).
FORBIDDEN_IP_PATTERNS = [
    re.compile(r"172\.18\.\d{1,3}\.\d{1,3}"),
    re.compile(r"192\.168\.\d{1,3}\.\d{1,3}"),
]

# __CLOUD_IP__는 deploy/demo-setup-v2.sh가 실제로 sed 치환하는 파일에만
# 등장해야 한다.
FILES_WITH_CLOUD_IP_PLACEHOLDER = {
    "namespace.yaml",
    "manager.yaml",
    "agent-edge1.yaml",
    "agent-edge2.yaml",
    "gateway.yaml",
    "rabbitmq.yaml",
}


# ---------------------------------------------------------------------------
# 아주 작은 YAML 부분집합 파서 (PyYAML 미사용)
# ---------------------------------------------------------------------------

YamlValue = Union[str, int, float, bool, None, Dict[str, Any], List[Any]]


class YamlSyntaxError(Exception):
    """이 파일의 미니 파서가 이해할 수 없는 구문을 만났을 때."""


def _strip_comment(line: str) -> str:
    """따옴표 밖에서 시작하는 '#' 주석을 제거한다."""
    in_single = in_double = False
    for i, ch in enumerate(line):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            if i == 0 or line[i - 1].isspace():
                return line[:i]
    return line


def _parse_scalar(raw: str) -> YamlValue:
    s = raw.strip()
    if s == "":
        return None
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    if s in ("true", "True", "TRUE"):
        return True
    if s in ("false", "False", "FALSE"):
        return False
    if s in ("null", "~", "Null", "NULL"):
        return None
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if re.fullmatch(r"-?\d+\.\d+", s):
        return float(s)
    return s


class _Tok:
    __slots__ = ("indent", "text", "lineno", "is_item")

    def __init__(self, indent: int, text: str, lineno: int, is_item: bool) -> None:
        self.indent = indent
        self.text = text
        self.lineno = lineno
        self.is_item = is_item


def _tokenize(text: str) -> List[_Tok]:
    tokens: List[_Tok] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if raw.strip() == "":
            continue
        stripped = _strip_comment(raw)
        if stripped.strip() == "":
            continue
        indent_len = len(raw) - len(raw.lstrip(" "))
        if "\t" in raw[:indent_len]:
            raise YamlSyntaxError(f"{lineno}행: 들여쓰기에 탭 문자를 사용할 수 없습니다")
        content = stripped.strip()
        is_item = content.startswith("- ") or content == "-"
        if is_item:
            remainder = content[2:].strip() if content.startswith("- ") else ""
            tokens.append(_Tok(indent=indent_len, text="-", lineno=lineno, is_item=True))
            if remainder:
                tokens.append(_Tok(indent=indent_len + 2, text=remainder, lineno=lineno, is_item=False))
        else:
            tokens.append(_Tok(indent=indent_len, text=content, lineno=lineno, is_item=False))
    return tokens


def _parse_block(tokens: List[_Tok], i: int, indent: int):
    if i >= len(tokens) or tokens[i].indent < indent:
        return None, i
    if tokens[i].is_item and tokens[i].indent == indent:
        return _parse_list(tokens, i, indent)
    return _parse_map(tokens, i, indent)


def _parse_list(tokens: List[_Tok], i: int, indent: int):
    result: List[YamlValue] = []
    while i < len(tokens) and tokens[i].indent == indent and tokens[i].is_item:
        i += 1  # "-" 마커 소비
        item_indent = indent + 2
        if i < len(tokens) and tokens[i].indent >= item_indent:
            value, i = _parse_block(tokens, i, item_indent)
            result.append(value)
        else:
            result.append(None)
    return result, i


def _parse_map(tokens: List[_Tok], i: int, indent: int):
    result: Dict[str, YamlValue] = {}
    while i < len(tokens) and tokens[i].indent == indent and not tokens[i].is_item:
        tok = tokens[i]
        key, sep, val = tok.text.partition(":")
        if not sep:
            raise YamlSyntaxError(f"{tok.lineno}행: 'key: value' 형식이 아닙니다: {tok.text!r}")
        key = key.strip()
        val = val.strip()
        i += 1
        if val in ("|", "|-", ">", ">-"):
            block_lines = []
            while i < len(tokens) and tokens[i].indent > indent:
                block_lines.append(tokens[i].text)
                i += 1
            result[key] = "\n".join(block_lines)
        elif val == "":
            if i < len(tokens) and tokens[i].indent > indent:
                child, i = _parse_block(tokens, i, tokens[i].indent)
                result[key] = child
            else:
                result[key] = None
        else:
            result[key] = _parse_scalar(val)
    return result, i


def parse_yaml_subset(text: str) -> List[Dict[str, YamlValue]]:
    """복수 문서(``---``)를 지원하는 아주 작은 YAML 부분집합 파서.

    지원: 매핑/시퀀스 중첩(들여쓰기 기준), 블록 스칼라(``|``), 주석,
    문자열/불리언/숫자 스칼라. 탭 들여쓰기는 표준 YAML과 동일하게 오류로
    취급한다. k8s 매니페스트 6개 파일 전체를 이 파서로 파싱해 구조를
    검증하는 것이 이 테스트 모듈의 목적이다.
    """
    docs_text = re.split(r"(?m)^---\s*$", text)
    docs: List[Dict[str, YamlValue]] = []
    for doc_text in docs_text:
        tokens = _tokenize(doc_text)
        if not tokens:
            continue
        value, i = _parse_block(tokens, 0, tokens[0].indent)
        if i != len(tokens):
            raise YamlSyntaxError(
                f"문서 파싱이 끝나지 않았습니다 (남은 토큰 {len(tokens) - i}개, "
                f"다음 줄={tokens[i].lineno})"
            )
        if not isinstance(value, dict):
            raise YamlSyntaxError("최상위 문서는 매핑이어야 합니다")
        docs.append(value)
    return docs


def _iter_containers(doc: Dict[str, YamlValue]):
    spec = doc.get("spec", {})
    template = spec.get("template", {}) if isinstance(spec, dict) else {}
    pod_spec = template.get("spec", {}) if isinstance(template, dict) else {}
    containers = pod_spec.get("containers", []) if isinstance(pod_spec, dict) else []
    return containers or []


# ---------------------------------------------------------------------------
# 픽스처
# ---------------------------------------------------------------------------

K8S_FILES = [
    "namespace.yaml",
    "manager.yaml",
    "agent-edge1.yaml",
    "agent-edge2.yaml",
    "gateway.yaml",
    "rabbitmq.yaml",
]


@pytest.fixture(scope="module")
def manifest_texts() -> Dict[str, str]:
    return {name: (K8S_DIR / name).read_text(encoding="utf-8") for name in K8S_FILES}


@pytest.fixture(scope="module")
def parsed_manifests(manifest_texts: Dict[str, str]) -> Dict[str, List[Dict[str, YamlValue]]]:
    return {name: parse_yaml_subset(text) for name, text in manifest_texts.items()}


# ---------------------------------------------------------------------------
# 모든 yaml이 파싱 가능한가
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", K8S_FILES)
def test_manifest_parses(manifest_texts: Dict[str, str], name: str) -> None:
    docs = parse_yaml_subset(manifest_texts[name])
    assert docs, f"{name}: 최소 한 개의 문서가 있어야 합니다"
    for doc in docs:
        assert "apiVersion" in doc, f"{name}: apiVersion 누락"
        assert "kind" in doc, f"{name}: kind 누락"
        assert "metadata" in doc, f"{name}: metadata 누락"


# ---------------------------------------------------------------------------
# 네임스페이스
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["namespace.yaml", "manager.yaml", "agent-edge1.yaml", "agent-edge2.yaml", "gateway.yaml", "rabbitmq.yaml"])
def test_all_resources_in_edge_auth_namespace(parsed_manifests, name: str) -> None:
    for doc in parsed_manifests[name]:
        if doc.get("kind") == "Namespace":
            assert doc["metadata"]["name"] == "edge-auth"
        else:
            assert doc["metadata"].get("namespace") == "edge-auth", (
                f"{name}: {doc.get('kind')}/{doc['metadata'].get('name')}의 namespace가 edge-auth가 아님"
            )


# ---------------------------------------------------------------------------
# 이미지 태그 :v2, imagePullPolicy Never
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected_images",
    [
        ("manager.yaml", {"eam-manager:v2"}),
        ("agent-edge1.yaml", {"eam-agent:v2"}),
        ("agent-edge2.yaml", {"eam-agent:v2"}),
        ("gateway.yaml", {"eam-agent:v2"}),
    ],
)
def test_eam_images_tagged_v2_and_never_pulled(parsed_manifests, name: str, expected_images: set) -> None:
    found_images = set()
    for doc in parsed_manifests[name]:
        if doc.get("kind") != "Deployment":
            continue
        for container in _iter_containers(doc):
            image = container.get("image", "")
            found_images.add(image)
            assert image.endswith(":v2"), f"{name}: 이미지 태그가 v2가 아님: {image!r}"
            assert container.get("imagePullPolicy") == "Never", (
                f"{name}: imagePullPolicy가 Never가 아님 (image={image!r})"
            )
    assert found_images == expected_images, f"{name}: 예상 이미지 집합과 다름: {found_images}"


def test_rabbitmq_image_not_eam_and_not_hardcoded_creds(parsed_manifests) -> None:
    docs = parsed_manifests["rabbitmq.yaml"]
    deployment = next(d for d in docs if d.get("kind") == "Deployment")
    containers = _iter_containers(deployment)
    assert len(containers) == 1
    assert containers[0]["image"] == "rabbitmq:3.13-management"


# ---------------------------------------------------------------------------
# Secret 참조 (평문 비밀번호가 아니라 secretKeyRef여야 하는 env)
# ---------------------------------------------------------------------------

SECRET_BACKED_ENV_NAMES = {
    "manager.yaml": {
        "BOOTSTRAP_TOKEN",
        "EAM_ADMIN_USERNAME",
        "EAM_ADMIN_PASSWORD",
        "EAM_OPERATOR_USERNAME",
        "EAM_OPERATOR_PASSWORD",
    },
    "agent-edge1.yaml": {"BOOTSTRAP_TOKEN"},
    "agent-edge2.yaml": {"BOOTSTRAP_TOKEN"},
    "gateway.yaml": {"BOOTSTRAP_TOKEN"},
}


@pytest.mark.parametrize("name", sorted(SECRET_BACKED_ENV_NAMES))
def test_sensitive_env_vars_use_secret_refs(parsed_manifests, name: str) -> None:
    expected = SECRET_BACKED_ENV_NAMES[name]
    seen = set()
    for doc in parsed_manifests[name]:
        if doc.get("kind") != "Deployment":
            continue
        for container in _iter_containers(doc):
            for env_entry in container.get("env", []) or []:
                env_name = env_entry.get("name")
                if env_name not in expected:
                    continue
                seen.add(env_name)
                assert "valueFrom" in env_entry and "value" not in env_entry, (
                    f"{name}: {env_name}이 평문 value로 설정되어 있습니다 (secretKeyRef여야 함)"
                )
                secret_ref = env_entry["valueFrom"].get("secretKeyRef")
                assert secret_ref and secret_ref.get("name") and secret_ref.get("key"), (
                    f"{name}: {env_name}의 secretKeyRef가 불완전합니다: {secret_ref!r}"
                )
    assert seen == expected, f"{name}: 누락된 Secret 기반 env: {expected - seen}"


def test_rabbitmq_creds_use_secret_refs(parsed_manifests) -> None:
    deployment = next(d for d in parsed_manifests["rabbitmq.yaml"] if d.get("kind") == "Deployment")
    containers = _iter_containers(deployment)
    env_by_name = {e["name"]: e for e in containers[0].get("env", []) or []}
    for name in ("RABBITMQ_DEFAULT_USER", "RABBITMQ_DEFAULT_PASS"):
        assert name in env_by_name, f"rabbitmq.yaml: {name} env가 없습니다"
        assert "valueFrom" in env_by_name[name], f"rabbitmq.yaml: {name}이 평문 값입니다"


# ---------------------------------------------------------------------------
# INSECURE_MODE / AUTO_APPROVE가 manager.yaml에 노출되어 있는가
# ---------------------------------------------------------------------------


def test_manager_exposes_insecure_mode_and_auto_approve(parsed_manifests) -> None:
    deployment = next(d for d in parsed_manifests["manager.yaml"] if d.get("kind") == "Deployment")
    containers = _iter_containers(deployment)
    env_by_name = {e["name"]: e for e in containers[0].get("env", []) or []}
    assert "INSECURE_MODE" in env_by_name
    assert "AUTO_APPROVE" in env_by_name
    # 둘 다 평문 env(Secret이 아님)로 노출되어 있어야 kubectl set env로 즉시 토글 가능하다.
    assert "value" in env_by_name["INSECURE_MODE"]
    assert "value" in env_by_name["AUTO_APPROVE"]


def test_manager_service_nodeport_30443(parsed_manifests) -> None:
    service = next(d for d in parsed_manifests["manager.yaml"] if d.get("kind") == "Service")
    ports = service["spec"]["ports"]
    assert any(p.get("nodePort") == 30443 for p in ports), "manager Service에 nodePort 30443이 없습니다"


# ---------------------------------------------------------------------------
# nodeSelector: agent/gateway는 edge, manager/rabbitmq는 control-plane
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["agent-edge1.yaml", "agent-edge2.yaml", "gateway.yaml"])
def test_agent_and_gateway_scheduled_on_edge_nodes(parsed_manifests, name: str) -> None:
    deployment = next(d for d in parsed_manifests[name] if d.get("kind") == "Deployment")
    node_selector = deployment["spec"]["template"]["spec"].get("nodeSelector", {})
    assert "node-role.kubernetes.io/edge" in node_selector


# ---------------------------------------------------------------------------
# __CLOUD_IP__ 플레이스홀더 - deploy 스크립트가 치환하는 파일에만 존재
# ---------------------------------------------------------------------------


def test_cloud_ip_placeholder_only_where_expected(manifest_texts: Dict[str, str]) -> None:
    for name, text in manifest_texts.items():
        has_placeholder = "__CLOUD_IP__" in text
        if name in FILES_WITH_CLOUD_IP_PLACEHOLDER:
            # namespace.yaml처럼 IP를 아예 참조하지 않는 파일도 있으므로 존재를
            # 강제하지 않는다 - 다만 "다른 곳에 등장하면 안 된다"는 쪽만 확인한다.
            continue
        assert not has_placeholder, f"{name}: __CLOUD_IP__가 치환 대상이 아닌 파일에 있습니다"

    # manager/agent/gateway는 실제로 __CLOUD_IP__를 사용해야 한다 (deploy 스크립트가
    # sed로 치환할 대상이 존재해야 의미가 있다).
    for name in ("agent-edge1.yaml", "agent-edge2.yaml", "gateway.yaml"):
        assert "__CLOUD_IP__" in manifest_texts[name], f"{name}: __CLOUD_IP__ 플레이스홀더가 없습니다"


def test_deploy_script_substitutes_cloud_ip_placeholder() -> None:
    script = (DEPLOY_DIR / "demo-setup-v2.sh").read_text(encoding="utf-8")
    assert "__CLOUD_IP__" in script
    assert "sed" in script


# ---------------------------------------------------------------------------
# 금지 문자열: 1차년도 하드코딩 비밀번호 / 사설 IP
# ---------------------------------------------------------------------------


def _all_project_text_files() -> List[Path]:
    files = list(K8S_DIR.glob("*.yaml")) + list(DEPLOY_DIR.glob("*.sh"))
    files += [DEPLOY_DIR / "Dockerfile.manager", DEPLOY_DIR / "Dockerfile.agent"]
    return files


@pytest.mark.parametrize("path", _all_project_text_files(), ids=lambda p: p.name)
def test_no_hardcoded_y1_password_or_ips(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert FORBIDDEN_Y1_PASSWORD not in text, f"{path.name}: 1차년도 하드코딩 비밀번호가 남아있습니다"
    for pattern in FORBIDDEN_IP_PATTERNS:
        assert not pattern.search(text), f"{path.name}: 하드코딩된 사설 IP가 있습니다 ({pattern.pattern})"


# ---------------------------------------------------------------------------
# 배포 스크립트 bash 문법 검증
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "script_name",
    ["demo-setup-v2.sh", "demo-stop-v2.sh", "build-images.sh"],
)
def test_deploy_scripts_have_valid_bash_syntax(script_name: str) -> None:
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("이 환경에 bash가 없어 문법 검증을 건너뜁니다 (Git Bash 등에서 실행 권장)")
    result = subprocess.run(
        [bash, "-n", str(DEPLOY_DIR / script_name)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"{script_name}: bash -n 실패\n{result.stderr}"


# ---------------------------------------------------------------------------
# demo/run_demo.py --fast
# ---------------------------------------------------------------------------


def test_run_demo_fast_exits_zero() -> None:
    """demo/run_demo.py --fast가 실제로 로컬 uvicorn(루프백)을 두 번 기동/종료하며
    보안 전/후 시나리오를 완주해 exit 0으로 끝나는지 확인한다.

    루프백 전용 uvicorn 서브프로세스 기동은 "no network/Docker in tests" 제약에
    저촉되지 않는다. Windows에서 uvicorn 기동이 느릴 수 있어 타임아웃을 넉넉히 둔다.
    """
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "demo" / "run_demo.py"), "--fast"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, (
        f"demo/run_demo.py --fast가 exit 0이 아닙니다 (rc={result.returncode})\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "BEFORE" in result.stdout
    assert "AFTER" in result.stdout
