"""eam.gateway: 사설망(private-IP) 하위 디바이스를 대신해 업링크하는 Edge Gateway.

:class:`eam.gateway.gateway.EdgeGateway` 자체는 하나의 Device로 Manager에
enroll하고, ``attach()``로 붙인 하위 가상 디바이스들은 Manager에 직접 등록하지
않는다(사설망 뒤에 있어 Manager에서 직접 도달 불가능하다는 시나리오). 하위
디바이스들의 페이로드는 배치로 묶여 게이트웨이 신원의 텔레메트리 1건으로
업링크된다 — Manager 측 API/스키마는 변경하지 않는다.
"""
