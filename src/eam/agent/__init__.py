"""eam.agent: Edge Device 측 Agent — 등록(enroll)/인증(token)/텔레메트리 전송.

FastAPI Manager를 두드리는 클라이언트는 :class:`eam.agent.agent.EdgeAgent` 하나이며,
httpx.AsyncClient의 transport를 주입할 수 있어 실서버 없이(``httpx.ASGITransport``)
Manager 앱을 인프로세스로 직접 호출하는 테스트/벤치마크가 가능하다.
"""
