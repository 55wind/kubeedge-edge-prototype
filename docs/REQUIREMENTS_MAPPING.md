# 요구사항(7개 수행항목) ↔ 산출물 매핑

Task 7(매핑 PPT)와 Task 8(최종 검수)이 기준으로 삼는 문서. 전체 `python -m pytest tests/ -q` 실행 결과 **147 passed**(2026-08-04 기준, 로컬 재현 가능)를 코드 산출물 완료의 근거로 삼는다.

| # | 수행 항목 | 코드 산출물 | 문서 산출물 | 테스트 | 데모 | 상태 |
|---|---|---|---|---|---|---|
| 1 | K8s(KubeEdge) 구동·성능 (1,000기 지연 검증) | `k8s/*.yaml`, `deploy/demo-setup-v2.sh`, `bench/run_bench.py`, `bench/model.py` | `docs/06-performance-plan.md`, `docs/perf/PERFORMANCE_REPORT.md` | `tests/test_manifests.py`, `tests/test_bench_model.py` | `deploy/demo-setup-v2.sh` (K8s 실환경), `bench/run_bench.py && python bench/report.py` (실측 재현) | **구현완료 + 문서화 + 시연가능**(로컬 시연은 즉시, K8s 실환경은 Multipass 필요) |
| 2 | 미들웨어 통신 방식 결정 | `src/eam/manager/app.py`(REST API), `k8s/rabbitmq.yaml`(선택적 대안, 미사용) | `docs/01-comm-method-decision.md`, `docs/02-manager-coexistence.md` | `tests/test_manager_api.py` | 해당 없음(설계 결정 문서) | **구현완료(하이브리드 구조로 반영) + 문서화** |
| 3 | 대규모(1,000기) 디바이스 인증 AAA ★핵심 | `src/eam/common/{pki,jws,audit}.py`, `src/eam/manager/{app,ca,rbac,store,schemas}.py`, `src/eam/simulator/{fleet,vdevice}.py` | `docs/04-rnr-interface.md`(인터페이스/인증서/JWT 규격), `docs/06-performance-plan.md` | `tests/test_pki.py`, `tests/test_jws.py`, `tests/test_audit.py`, `tests/test_manager_api.py`, `tests/test_rbac.py`, `tests/test_simulator.py` | `python -m eam.simulator.fleet --n 200`(실측), `demo/run_demo.py`(AAA 흐름 시연) | **구현완료 + 문서화 + 시연가능** |
| 4 | KubeEdge 연동 구조 적용 | `k8s/{manager,agent-edge1,agent-edge2,gateway,namespace}.yaml`, `deploy/demo-setup-v2.sh`(CloudCore/EdgeCore 구축) | `docs/05-kubeedge-integration.md`(4계층 흐름도 mermaid 포함) | `tests/test_manifests.py` | `deploy/demo-setup-v2.sh` + `demo/DEMO_SCENARIO.md` §2 | **구현완료 + 문서화 + 시연가능**(K8s 실환경은 Multipass 필요, 로컬은 즉시) |
| 5 | 네트워크 구성·주소 체계 (공인 IP) | `src/eam/gateway/gateway.py`, `k8s/gateway.yaml` | `docs/03-network-addressing.md` | `tests/test_gateway.py` | `demo/DEMO_SCENARIO.md` §2.2(게이트웨이 집선 확인) | **구현완료 + 문서화 + 시연가능** |
| 6 | 보안 모듈–프레임워크 R&R | `src/eam/manager/app.py`(공개 계약), `src/eam/manager/schemas.py` | `docs/04-rnr-interface.md` | `tests/test_manager_api.py`(엔드포인트 계약 검증) | 해당 없음(문서 산출물) | **문서화 완료** |
| 7 | 시연 (보안 적용 전·후 비교) | `demo/run_demo.py`, `src/eam/manager/app.py`(`INSECURE_MODE`) | `demo/DEMO_SCENARIO.md` | `tests/test_manifests.py`(`run_demo.py --fast` exit 0 검증 포함) | `python demo/run_demo.py` / `--fast`, K8s: `kubectl set env ... INSECURE_MODE=true\|false` | **구현완료 + 문서화 + 시연가능**(로컬 검증 완료: exit 0) |

## 커버리지 상태 범례

- **구현완료**: 코드가 존재하고 pytest로 검증됨.
- **문서화**: `docs/` 산출물이 요구사항을 표 중심으로 설명하고 결론을 명시함.
- **시연가능**: 로컬(`demo/run_demo.py`) 또는 K8s(`deploy/demo-setup-v2.sh`) 경로로 실행 가능한 절차가 문서화됨(K8s 경로는 Windows 로컬에 Multipass가 없어 `bash -n` 문법 검증까지만 수행, 실행 자체는 ETRI 테스트베드에서 확인 필요).

## Task 7(PPT) 관련 참고

`ppt/build_ppt.py`, `ppt/ETRI_2차년도_수행항목_매핑.pptx`는 본 Task 6 시점에는 아직 생성되지 않았다(Task 7 범위). 위 표의 코드/문서/테스트 경로는 Task 7 슬라이드 5~11("수행항목 1~7 각 1장")의 산출물 근거 자료로 그대로 재사용될 예정이다.

## Task 8(최종 검수) 관련 참고

Task 8은 본 표를 기준으로 (a) 전체 `pytest tests/ -q` 재실행, (b) `python demo/run_demo.py --fast` exit 0 확인, (c) `python ppt/build_ppt.py`(Task 7 완료 후) 재실행 검증, (d) whole-branch 코드리뷰로 7개 항목 커버리지를 최종 대조한다.
