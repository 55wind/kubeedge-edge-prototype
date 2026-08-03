"""eam.simulator: 가상 디바이스(VirtualDevice) 수명주기 + 플릿(fleet) 부하 시뮬레이터.

``vdevice.py``는 :class:`eam.agent.agent.EdgeAgent`를 래핑해 enroll -> 인증 ->
텔레메트리 N회 전송의 수명주기를 실행하고 단계별 지연시간(ms)을 기록한다.
``fleet.py``는 그런 가상 디바이스 N대를 ``asyncio.Semaphore``로 동시성을 제한해
동시 실행하고 단계별 p50/p95/p99/mean/max 및 성공/실패 수를 집계한다
(``python -m eam.simulator.fleet``로 CLI 실행 가능).
"""
