"""bench: 성능 벤치마크 + 1,000기 외삽 모델 (Task 4).

- ``bench.run_bench``: N-스윕 벤치마크 실행 CLI (in-process ASGI, 실네트워크 없음).
- ``bench.model``: 순수 함수 모음 - M/M/c 대기행렬 근사 + 다항 최소제곱 보조 적합.
- ``bench.report``: 최신 벤치마크 JSON을 읽어 한국어 리포트(MD)+PNG 차트 생성.
"""
