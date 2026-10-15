# HaIot TODO (2026-10-15 기준)

다음 세션에서 이 파일부터 읽고 진행한다. 배경 문서: `제어불량_원인분석.md`, `개선방향_리뷰_워크플로우.md`, `COMMAX_패킷구조_분석.md`, `현관_방문자사진_NAS브리지_계획.md`.

## 현재 상태 요약
- HA: http://cwygp.duckdns.org:8123 (HA 2026.10.x). 저장소: github.com/cwygp1/HaIot (public, HA 애드온 스토어에 등록됨, repo slug `e647ee34`).
- 월패드 연동: EW11(192.168.1.3, MQTT 모드) + 애드온 `e647ee34_commax_test` v1.3.0. **EW11은 2026-10-15 08:10부터 오프라인**(ping 무응답, 전원/Wi-Fi 의심). 그래서 코맥스 엔티티는 지금 사용불능 상태.
- 옛 애드온 `0026d5a4_commax_test`(v1.2.3)는 중지, boot=manual. HAKorea "Wallpad Controller with RS485"도 떠 있지만 시리얼 없음(중지 권장).
- 배선: COMMAX 세대 단자함 허브 → RJ45 랜선 → 월패드. 중간에 랜스타 RJ45 1:2 커플러로 분기, **5·6번 두 가닥만**(RS485 A/B) EW11에 연결. 바로 옆에 NAS.
- 실측 장치: 조명 8(1~5 거실, 6 = 거실 전체 켜기/끄기, 7·8 = 안방 조명1·2), 난방 4, 일괄소등 1(byte5 0x15), 가스 1(90 50 50), 환기 1(타이머 패킷 확인됨). 콘센트 없음.

## 1. [대기] EW11 → KinCony KC868-A2 + ESPHome 교체  ← 장비 도착하면 시작
사용자가 주문 예정/주문함. 도착하면 보드 모델명(A2 / A2v3) 먼저 확인.
- [ ] 보드 모델 확인. A2: LAN8720 (MDC 23, MDIO 18, CLK GPIO17_OUT, phy 0), RS485 TX GPIO32 / RX GPIO35. A2v3는 ESP32-S3 + W5500이라 핀 다름(devices.esphome.io/devices/KinCony-KC868-A2v3).
- [ ] HA에 ESPHome Device Builder 애드온 설치.
- [ ] ESPHome 설정 작성 (`esphome/commax.yaml`로 저장소에 커밋): 기기 이름 `commax`, ethernet, uart 9600 8N1, eigger `uartex`(reference/eigger/eigger_commax.yaml 기반)에 우리 집 값 반영
  - 조명 1~8 (`light.commax_light1..8` 유지되게 이름 Light1..), 일괄소등(byte5 0x15), 난방 1~4(BCD, 0x83 heating), 가스(잠금만, 상태 90 50 50/A0 A0), 환기(풍량 1~3, 타이머 `78 ID 03 HH MM 00 00`, 상태 `F6 04 ID spd flag HH MM`)
  - 고장 조명1 안전장치(켜지면 5초 뒤 끄기)를 펌웨어 안에도 넣을지 결정
  - rx_length 8, checksum add, rx_priority loop, tx_delay/tx_timeout/tx_retry_cnt 실측 기반
  - web_server 켜기(HA 없이도 휴대폰 제어)
- [ ] 첫 굽기: USB로 PC 연결 → 이후 OTA.
- [ ] 배선: 커플러 빈 포트 → RJ45 단자대 어댑터 → 5번=A, 6번=B → A2 RS485 단자. A2 RJ45 → 집 공유기(COMMAX 허브 아님). 전원은 새 12V 1A 이상 어댑터(KC 인증). 절연은 불필요로 결정(사용자 동의). 케이스 없음 → 스페이서/정션박스.
- [ ] 이전: 애드온 `e647ee34_commax_test`에서 `purge_discovery: true`로 MQTT 엔티티 제거 후 애드온 중지 → ESPHome 엔티티 ID를 `light.commax_light*` 등으로 맞추기 → 대시보드·자동화(`automation.imsi_geosil_jomyeong1_gojang_kyeojimyeon_5co_dwi_ggeugi`) 확인.
- [ ] 동작 확인: 조명/난방/환기/타이머/가스 명령 성공률 측정.
- [ ] EW11 회수, 옛 애드온들 제거 정리.

## 2. [보류] 엘리베이터 호출
- RS485에는 엘리베이터 패킷(0x23/0x26)이 없음. 호출은 월패드가 단지망 엘리베이터 서버로 보내는 HTTP/SOAP `setEvStatus`(조회 `getEvStatus`) — jhkwon19/commax_wallpad 참고.
- 대시보드 "엘리베이터 호출" 카드는 지금 동작 안 함(옛 RS485 패킷). 숨기거나 "준비 중" 표시 검토.
- [ ] 월패드 화면(메인보드) 수리 후 진행.
- [ ] NAS에 두 번째 NIC(또는 USB 랜)로 단지망 쪽 연결, 월패드 이더넷 트래픽 캡처 → 호출 요청 확보 → NAS에서 재현 → HA 버튼.
- 같은 배선으로 현관 방문자 사진(`현관_방문자사진_NAS브리지_계획.md`)도 함께 검토.

## 3. [선택] 기타
- [ ] 솔리티 도어락(`lock.hyeongwan_doeorag`, HACS tpgi2013-hue/Smart-Solity-Doorlock v1.7.3, HA용 공유 계정): 대시보드 "잠그기" 버튼 실제 동작 확인 결과 받기. 문 실시간 감지는 지그비 문센서 추가 검토.
- [ ] 거실 조명1 수리되면 임시 자동화 끄고 대시보드 카드 원복.
- [ ] HA 코어 로그의 `CURRENT_HVAC_HEAT` ImportError (오래된 커스텀 climate 통합) 정리.
- [ ] 안방화장실 습도계1 사용불능(배터리/지그비) 점검.
- [ ] (ESPHome 이후) HA Matter Bridge로 외부 플랫폼 노출 검토.
