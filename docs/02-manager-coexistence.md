# 02. 한 서버 내 다수 Manager 공존 규칙

**수행항목 2(미들웨어 통신 방식 결정)** 부속 문서. `docs/01-comm-method-decision.md`에서 결정한 "데이터·인증은 별도 REST 채널" 구조를 전제로, 하나의 물리 서버(또는 하나의 K8s 클러스터) 안에 **여러 Manager 인스턴스**가 공존해야 하는 상황(사이트별 분리, 부하 분산, 테스트/운영 격리)에서 필요한 분리 규칙을 정리한다.

## 1. Manager 인스턴스가 이미 격리 가능하게 설계된 근거

`src/eam/manager/app.py`의 `create_app()`은 모듈 전역 `app` 싱글턴이 아니라 **팩토리 함수**이며, 다음 세 경로를 명시적으로 주입받는다.

```python
def create_app(*, certs_dir=None, store_db_path=None, audit_db_path=None) -> FastAPI: ...
```

각 값이 생략되면 `eam.common.config.load_config()`가 읽는 환경변수(`CERTS_DIR`, `DB_URL`)로 대체된다. 이 설계 덕분에 **동일 프로세스/서버 안에서도 서로 다른 `CERTS_DIR`/`DB_URL`/포트를 가진 Manager 인스턴스를 몇 개든 동시에 띄울 수 있다** — 실제로 `bench/run_bench.py:_build_inprocess_app`이 N-스윕 각 단계마다 격리된 `tmp_dir`로 새 Manager 앱을 반복 생성하는 것이 이 능력의 실증 사례다.

## 2. 네임스페이스·포트·역할 분리 규칙

| 축 | 규칙 | 근거/현재 구현 |
|---|---|---|
| **K8s 네임스페이스** | Manager 배포 단위(사이트/조직)당 하나의 네임스페이스 사용을 원칙으로 한다. 현재 데모는 `edge-auth` 단일 네임스페이스(`k8s/namespace.yaml`)를 쓰지만, 다수 Manager가 필요해지면 `edge-auth-<site>` 형태로 네임스페이스를 나눠 RBAC(K8s `Role`/`RoleBinding`)·리소스 쿼터를 사이트별로 격리한다. |
| **포트** | 각 Manager 인스턴스는 고유한 `containerPort`/`NodePort` 쌍을 갖는다. 현재 단일 인스턴스는 `containerPort: 8443` → `nodePort: 30443`(`k8s/manager.yaml`). 두 번째 인스턴스를 같은 클러스터에 둘 경우 `nodePort: 30444`처럼 순차 할당하고, 각 인스턴스의 `CERTS_DIR`/`DB_URL`도 별도 볼륨으로 분리해야 한다(같은 CA/DB를 공유하면 인증서 신뢰 루트가 뒤섞인다). |
| **역할(인증/데이터/관리)** | Manager 내부적으로는 이미 `RBAC_MATRIX`(`src/eam/manager/rbac.py`)가 엔드포인트를 역할별로 분리한다: `device`(텔레메트리 전송만), `operator`(장치 목록 조회), `admin`(승인/폐기/감사조회). 다수 Manager 공존 시에도 이 3역할 체계를 그대로 유지해, 인스턴스마다 별도의 역할 체계를 만들지 않는다. |

RBAC 매트릭스(현재 구현, `src/eam/manager/rbac.py`):

```python
RBAC_MATRIX: Dict[Tuple[str, str], FrozenSet[str]] = {
    ("POST", "/api/v1/telemetry"): frozenset({"device", "admin"}),
    ("GET", "/api/v1/devices"): frozenset({"operator", "admin"}),
    ("POST", "/api/v1/devices/{id}/approve"): frozenset({"admin"}),
    ("POST", "/api/v1/devices/{id}/revoke"): frozenset({"admin"}),
    ("GET", "/api/v1/audit"): frozenset({"admin"}),
}
```

`/devices/register`, `/auth/token`, `/auth/operator`, `/healthz`는 RBAC 매트릭스에 없고 각자의 비즈니스 로직(bootstrap token 대조, 인증서 체인 검증, operator 자격증명 대조)으로 접근을 통제한다(`rbac.py` 모듈 docstring 참고).

## 3. 할당·분배 규칙 (디바이스 그룹 → Manager 샤딩)

1,000기 규모에서 단일 Manager로도 SLA를 충족한다는 것이 `docs/06-performance-plan.md`(및 `docs/perf/PERFORMANCE_REPORT.md`)의 결론이지만, 사이트가 여러 곳으로 물리적으로 분산되거나 더 큰 규모로 확장할 경우를 대비해 다음 샤딩 규칙을 권고한다.

| 샤딩 키 | 규칙 |
|---|---|
| `site` | `RegisterRequest.site`(`src/eam/manager/schemas.py`) 값을 기준으로 디바이스를 사이트별 Manager에 배정. Agent/Gateway 쪽 `DEVICE_SITE` 환경변수(`k8s/agent-edge1.yaml`: `factory-A`, `agent-edge2.yaml`: `factory-B`)가 이미 이 축을 구분하고 있다. |
| `group` | `RegisterRequest.group` 값을 부하 분산 세부 단위로 사용(예: `sensors`, `gateways`). 게이트웨이(`k8s/gateway.yaml`: `group=gateways`)처럼 트래픽 패턴이 다른 디바이스군을 별도 Manager로 분리할 수 있다. |
| **라우팅 방식** | Agent/Gateway는 `MANAGER_BASE_URL` 환경변수(`src/eam/agent/__main__.py`, `src/eam/gateway/__main__.py`)로 대상 Manager를 지정하므로, 샤딩은 **배포 시점의 값 주입**만으로 구현되며 Manager 코드 변경이 필요 없다. 클러스터 내부 라우팅(예: 사이트별 Ingress path)은 별도 검토 대상이다(`docs/04-rnr-interface.md` §6 참고). |

## 4. 우선순위 규칙

여러 Manager가 공존할 때 자원 경합(K8s 노드 CPU, DB I/O) 상황에서의 우선순위:

1. **admin/operator 트래픽 > device 트래픽**: 승인·폐기·감사조회(admin) 요청은 운영 개입이 필요한 경로이므로 device의 대량 텔레메트리보다 지연에 민감하다고 가정한다. K8s `PriorityClass`를 Manager Pod에 지정하는 것을 권고(현재 매니페스트에는 미적용 — ETRI 협의 항목).
2. **인증(`/auth/token`) > 텔레메트리(`/telemetry`)**: 인증 실패는 이후 모든 통신을 차단하므로, 인증 스톰 상황에서 인증 엔드포인트의 처리 용량을 우선 확보해야 한다(`docs/06-performance-plan.md`의 확장 전략과 연결).
3. **감사 기록(accounting)은 항상 최우선**: `audit_and_mode_middleware`(`app.py`)는 모든 요청(성공/실패/미처리 예외 포함)에 대해 감사 행을 기록하도록 설계돼 있어, 다수 Manager 환경에서도 이 미들웨어를 절대 비활성화하지 않는 것을 원칙으로 한다.

## 5. 결론

- 코드 수준에서는 이미 `create_app()` 팩토리 패턴으로 다수 Manager 공존이 가능하도록 설계돼 있다(격리된 `CERTS_DIR`/`DB_URL`).
- 배포 수준에서는 네임스페이스(사이트별) · 포트(NodePort 순차 할당) · 역할(RBAC 3역할 고정) 세 축으로 분리하고, `site`/`group` 필드로 디바이스를 샤딩한다.
- 현재 1,000기 규모(§`docs/06`)에서는 단일 Manager 레플리카로 충분하므로, 다수 Manager 공존은 **다중 사이트로 물리적 분산이 필요해질 때** 적용하는 확장 옵션으로 남겨둔다.
