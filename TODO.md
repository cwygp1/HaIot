# HaIot TODO (2026-10-15 기준)

다음 세션에서 이 파일부터 읽고 진행한다. 배경 문서: `제어불량_원인분석.md`, `개선방향_리뷰_워크플로우.md`, `COMMAX_패킷구조_분석.md`, `현관_방문자사진_NAS브리지_계획.md`.

## 현재 상태 요약
- HA: http://cwygp.duckdns.org:8123 (HA 2026.10.x). 저장소: github.com/cwygp1/HaIot (public, HA 애드온 스토어에 등록됨, repo slug `e647ee34`).
- 월패드 연동: EW11(192.168.1.3, MQTT 모드) + 애드온 `e647ee34_commax_test` v1.3.0. **EW11은 2026-10-15 08:10부터 오프라인**(ping 무응답, 전원/Wi-Fi 의심). 그래서 코맥스 엔티티는 지금 사용불능 상태.
  - 19:00 진단: Orbi(SXR50 본체)는 EW11을 2.4GHz, 신호 -30, IP .3으로 "접속 중"이라 보고. 그러나 HA에서 .3은 ping·telnet·웹 모두 무응답이고, 서브넷 전체에 telnet 여는 기기는 NAS(.2)뿐. → 신호·IP 변경 문제가 아니라 **EW11이 Wi-Fi에 붙은 채 내부가 멈춘 상태**로 판단. 조치: EW11 전원 완전 차단 10초 후 재투입. 반복되면 고장 이력 있는 전원 어댑터 교체 또는 KC868-A2로 조기 전환.
  - 19:07 EW11 전원 재투입 → Orbi에 새로 접속(위성↔본체 오감)했지만 여전히 무응답. 추가로 **HA에서 Wi-Fi 기기 전부 무응답**(ESP .13, 크롬캐스트·네스트허브, 카메라 .15, 로봇청소기 .6, 폰). 공유기 .1·NAS .2(유선)만 응답. Cast 스피커 3대도 15:50부터 unavailable. → EW11 고장보다 **Orbi 유선↔무선 통신 문제** 가능성이 큼. 조치 순서: Orbi 본체 재부팅 → 그래도 안 되면 EW11 초기화(Reload 핀을 GND에 단락, 기본 AP `EW11_xxxx`, 10.10.100.254, admin/admin).
  - **20:36 해결**: EW11 초기화 후 재설정(STA ORBI, MQTT 192.168.1.28:1883, ew11-mqtt) + Orbi 재부팅 → MQTT 접속, 코맥스 엔티티 전부 복구. 원인은 Orbi의 유선↔무선 통신 막힘(Wi-Fi 기기 전체가 HA에서 안 보였음). 재발 시 Orbi 재부팅부터.
  - **20:38 재발 → 진짜 원인: Orbi 펌웨어 V4.3.2.100 → V4.3.2.200 업데이트**(HA 업데이트 버튼, 10-15 08:12경 설치). 이후 유선↔무선 통신이 Orbi 재부팅 후 약 2분만 되고 끊김. 넷기어 커뮤니티에 같은 증상 + 4.3.2.100 롤백으로 해결 사례. 조치: 넷기어 지원 페이지에서 4.3.2.100 받아 Orbi 웹 UI에서 수동 설치(위성 먼저), 넷기어 권고상 다운그레이드 후 초기화 필요할 수 있음. KC868-A2(유선)는 이 문제 영향 없음.
  - **20:56 롤백 완료 → 해결 확인**: 4분 넘게 Wi-Fi 기기·EW11 모두 정상. HA의 Orbi 펌웨어 업데이트 엔티티(`update.6rw2245ca14a8_firmware`)는 비활성화함. Orbi 자동 업데이트가 다시 V4.3.2.200을 깔면 재발하므로 주의.
  - HA에 진단용으로 `sensor.hf_ew11_access_point_mac`, `_signal_strength`, `_link_type` 활성화함.
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

## 2. 엘리베이터 — RS485로 호출·층 표시 동작 확인 (2026-10-15 실측)
- 호출: 현관 버튼 → 일괄스위치가 `20 01` 조회에 `A0 01 01 00 08 15 00 BF`로 응답. HA에서 같은 패킷 주입해도 호출됨(1층→14층 도착 확인).
- 접수: 월패드 `22 01 40 07 00 00 00 6A` → 스위치 `A2 01 01 00 00 00 00 A4`.
- 층: 접수 후 도착까지 `23 01 [층] 00 00 00 00 CS`가 약 1초 간격. **층 바이트는 BCD**(01=1층 … 10=10층, 14=14층). 호출이 없을 때는 버스에 층 정보 없음(월패드 화면 층 표시는 단지망 getEvStatus로 추정).
- 변형 실험(HA mqtt.publish로 ew11/send 직접 송신): `A0 00 01 …08 15 00 BE` 호출됨, `A0 01 00 …BE`·`A0 00 00 …BD` 무시 → 필수는 byte2=01(스위치 ID)+byte4=08. byte1(일괄소등 전원)은 무관. 호출에 목적지 층 필드 없음.
- `23` 송출은 도착 후 약 70초까지 이어지고 멈춤. 동은 최소 19층(BCD 19 관측).
- 애드온 v1.3.2 반영(BCD 해석, ack `22..40|23|26`).
- [ ] 지하층 값 확인: 엘리베이터가 지하에 있을 때 호출 → 첫 `23` 값 기록(v1.3.2는 BCD가 아니면 `raw XX`로 표시). 우리 집 14층.
- [ ] 호출 재전송: 현재 접수까지 1초 안에 3~5회 송신됨 → EV만 재전송 간격을 늘릴지 검토.
- [ ] 자동화 아이디어: 평일 07~09시, 재실 중 현관문 열림 → 엘리베이터 호출. 도어락(`binary_sensor.hyeongwan_doeorag_door`) 기록 며칠 쌓인 뒤 열림 반영 지연 확인 후 결정. 쿨다운 60~120초.
- 현관 방문자 사진(`현관_방문자사진_NAS브리지_계획.md`)은 단지망 배선 쪽에서 별도 검토.

## 3. [선택] 기타
- [ ] 솔리티 도어락(`lock.hyeongwan_doeorag`, HACS tpgi2013-hue/Smart-Solity-Doorlock v1.7.3, HA용 공유 계정): 대시보드 "잠그기" 버튼 실제 동작 확인 결과 받기. 문 실시간 감지는 지그비 문센서 추가 검토.
- [ ] 거실 조명1 수리되면 임시 자동화 끄고 대시보드 카드 원복.
- [ ] HA 코어 로그의 `CURRENT_HVAC_HEAT` ImportError (오래된 커스텀 climate 통합) 정리.
- [ ] 안방화장실 습도계1 사용불능(배터리/지그비) 점검.
- [ ] (ESPHome 이후) HA Matter Bridge로 외부 플랫폼 노출 검토.
