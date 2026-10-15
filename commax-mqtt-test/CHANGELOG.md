수정 사항 Changelog
==================
v 1.3.2 (2026-10-15) — 엘리베이터 실측 반영
-------
- 실측: 현관 호출 버튼 = 일괄스위치가 `20 01` 조회에 `A0 01 01 00 08 15 00 BF`로 응답. 월패드가 접수하면 `22 01 40 07 00 00 00 6A`(스위치 응답 `A2 01 01 …`) 후 `23 01 [층]`을 약 1초 간격으로 송출.
- HA에서 `A0 01 01 00 08 15 00 BF` 주입으로 호출 성공 확인 (1층→10층 도착).
- 층 바이트를 BCD로 해석(01=1층, 10=10층), 잘못된 BasementFloor 보정 제거. 지하층은 미확인이라 BCD 아닌 값은 `raw XX`로 표시.
- EV ack에 월패드 호출 접수 패킷 `22..40` 추가 → 불필요한 재전송 감소.
v 1.3.1 (2026-10-15)
-------
- 엘리베이터 층 패킷(23/26) 원문을 처음 볼 때마다 INFO 로그로 남김 (EV 비활성 시에도 미등록 헤더로 기록). 층 인코딩 실측용.

v 1.3.0 (2026-10-13) — 리뷰 워크플로우 권고 반영 (개선방향_리뷰_워크플로우.md)
-------
P0
1. 수신 스트림 슬라이딩 재동기화 + 이월 버퍼(100ms) + 알려진 헤더 필터: 앞 바이트가 깨져도 뒤따르는 ack를 잃지 않음
2. 명령응답 헤더(B1/84/A2/91/FA/F8)도 상태로 반영 → 명령 직후 ~50ms에 HA 상태 갱신
3. 분당 송신 상한 도달 시 폐기 대신 대기, 재전송은 카운트 제외, 명령 TTL(cmd_ttl_sec, 10s) 추가
P1
4. 송신 스케줄러: "최소 gap" → 송신 창 [send_gap_ms, +send_window_ms] + one-in-flight(ack_wait_ms) + retry 150ms. 창을 지나치면 다음 수신까지 보류(버스가 1초 이상 조용하면 즉시 송신). last_rx/last_tx 분리
5. 상태 매칭을 마스크 방식으로: 조명/일괄소등 [헤더][값][ID](byte5 변형 무관), 가스 bit7, 난방 bit0 + action(off/idle/heating), 환기 auto/night 프리셋 표시, setTemp ack `8.`
6. 워치독: 수신 시각만 기준. 재시작 전 Light1 조회 프로브로 "EW11 생존/월패드 조회 중단"을 구분하고, telnet 포트 확인 후에만 재시작, 백오프 300→600→…초. 2단 availability(`commax/status` + `commax/bus`)
7. paho 콜백 예외 보호, 접속/끊김 센티널로 루프에서 처리, 재접속 지연 1~30s, 네트워크 스레드 사망 시 프로세스 종료(HA 감시로 재시작), 태스크 자동 재시작
8. run.sh `exec`, SIGTERM 처리로 종료 시 `commax/status offline` 즉시 발행, client_id에 호스트명 접미사
9. telnetlib → socket, paho CallbackAPIVersion.VERSION2, 베이스 이미지 python:3.12
10. shift_id/ID 읽기 바이트 단위 (ID 16 이상 안전)
11. 진단: `commax/diag/state` JSON(60s) + 센서 5개(첫 시도 성공률, ack 지연 p50, 조회 주기, 폐기 수, 대기 명령) + `binary_sensor.commax_wallpad_alive`. 조회+응답 묶임 비율이 50% 넘으면 EW11 Gap Time 10ms 안내
12. discovery 보강: `default_entity_id`(HA 2025.10+에서 object_id 대체), device identifiers 배열/model/sw, origin, climate act_t·precision·temp_unit, fan percentage(1~3)+preset(auto/night), gas binary_sensor, 옛 토픽 자동 제거(`/share/commax_discovery_published.json`), `purge_discovery` 옵션
13. 테스트를 pytest + 가상 시계로 전환 (`tests/test_v13.py`, 27개)
P2
14. bus_master 폴백(auto/on/off): 외부 조회가 master_after_sec(10s) 없으면 애드온이 150ms 간격으로 조회를 돌려 버스를 유지. 월패드 조회가 재개되면 즉시 종료
15. elfin_log는 raw_log_minutes(10분) 후 자동 종료
16. config: password 타입, 빈 기본값, armhf 제거, bus_master/진단 옵션

v 1.2.5 (2026-10-13)
-------
1. 환기 타이머 지원. 실측으로 확인한 패킷: 명령 `78 ID 03 HH MM 00 00`(2진수 시/분, 00 00 = 해제), 상태 `F6 04 ID spd [플래그] [HH] [MM]`(1분마다 감소), 응답 `F8 00 ID spd HH MM`.
   - `select.commax_fanN_timer` (off/30/60/90) 으로 설정, `sensor.commax_fanN_timer_remaining` (분) 으로 남은 시간 표시
   - MQTT: `commax/FanN/timer/command` (off|분), `commax/FanN/timer/state`, `commax/FanN/timer_remaining/state`
2. 기본 send_gap_ms/poll_wait_ms 60 (CHANGELOG 정정)

v 1.2.4 (2026-10-12, cwygp1 패치)
-------
원인 분석: 저장소 루트 `제어불량_원인분석.md`
1. MQTT discovery/상태 메시지를 retain으로 발행. `homeassistant/status`가 online이 되면(HA 재시작) discovery와 상태를 재발행 → HA 재시작 후 엔티티가 unavailable로 남는 문제 해결
2. `commax/status` availability(LWT) 추가 → 애드온이 죽으면 엔티티가 unavailable로 표시됨
3. 장치 파일명 불일치 수정: `/share/commax_found_device.json` 저장·읽기 통일 (`commax_found_device_new.json`이 있으면 우선 사용). 검색 결과가 0개면 저장하지 않고 30초 후 재검색
4. 송신 조건을 "마지막 수신 후 100ms" → `send_gap_ms`(기본 60ms, 실측 튜닝)로. 마지막 패킷이 월패드 조회(헤더<0x80)였으면 `poll_wait_ms`(60ms)까지 기다려 장치 응답과의 충돌을 피함. 재전송은 `retry_interval_ms`(300ms) 간격, `max_retry`(5)회 후 폐기 시 WARNING 로그
5. 상태를 받기 전에도 HA 명령을 송신 (이전엔 상태 없는 장치의 명령을 무시)
6. 명령 성공 판정을 정규식으로: 상태 헤더/명령응답 헤더(+0x80) 모두 인정, 난방은 온도 바이트 완전일치 요구 제거(설정온도 변경 시 설정값만 확인)
7. EW11 재시작 워치독 완화: `ew11_restart_after_sec`(60초) 무신호 후, `ew11_restart_min_interval_sec`(300초)마다 최대 1회. telnet timeout 3초, 별도 스레드로 실행해 송신 루프를 막지 않음. `elfin_server`가 비어 있으면 재시작 안 함
8. 환기(Fan) `'fan'` 오타 수정, 풍량 제어/상태 복구. Fan discovery를 preset_mode(low/medium/high)로
9. 미등록 헤더 패킷은 헤더별 1회 INFO 로그, KeyError 제거. 패킷 해석 실패는 WARNING
10. paho 콜백 → asyncio 큐로 전달하는 단일 이벤트 루프 구조. 베이스 이미지 python:3.11-slim-bookworm, paho-mqtt 2.x
11. discovery 페이로드에서 잘못된 `device_class`(light/climate/fan) 제거, 콘센트 전력 센서는 device_class power + W
12. `tx_enable`(수신 전용 모드)과 `max_tx_per_min`(분당 송신 상한) 안전장치 추가
13. 옵션 추가: `mqtt_port`, `send_gap_ms`, `retry_interval_ms`, `max_retry`, `scan_seconds`, `ew11_restart_after_sec`, `ew11_restart_min_interval_sec`. `save_unregistered_signal` 삭제(동작하지 않던 옵션)

v 1.2.3
-------
1. Python3 Connect 변경으로 인한 소스 수정


v 1.2.1
-------
1. selenium 패키지 삭제하고 telnet 으로 변경 (elfin ew11 설정에서 telnet을 켜두어야 함)
2. apline에서 buster로 이미지 변경

v 1.2.0
-------
1. elfin ew11 장치가 응답이 없을 시 ew11 기기를 재시작 합니다. ew11 기기의 ip, 아이디와 패스워드가 필요합니다.
2. 출력 메시지를 한글로 변경

v 1.1.3
-------
1. FAN 활성화 시 에러 수정
2. 자잘한 오타 수정

v 1.1.2
-------
180줄 에러 해결

v 1.1.1
-------
1. 엘리베이터를 추가하면 나오는 오류 수정

v 1.1.0
-------
큰 변화가 있어서 가운데 숫자가 업데이트 되었습니다. 기기의 재검색이 필요합니다. /share 폴더의 기기 파일을 삭제하세요.
1. 패킷을 8비트(16자리)로 끊어서 확인하도록 변경 (statePrefix가 필요없어져서 삭제)
2. 엘리베이터 층수 추가 (지하층 수를 BasementFloor에 입력)
3. 가스 패킷을 자동으로 찾아주는 옵션 삭제 (이제는 직접 패킷을 검색해서 json 파일을 수정하여야 합니다.)
4. 10초간 mqtt 신호가 없을 경우 경고 출력 (ew11 먹통 확인 가능)
5. 등록된 기기 신호이지만 JSON 파일 등록이 안되어 있는 신호의 경우 경고 출력
6. 현재 등록되지 않은 기기 신호를 파일로 저장하는 옵션이 작동하지 않습니다. 추후에 고쳐넣겠습니다.

v 1.0.6
-------
1. 콘센트 off 시 전력량이 0이 되지 않는 오류 수정

v 1.0.5
-------
1. 콘센트 onoff 추가 (기기 재검색이 필요합니다. /share 폴더의 기기 파일을 삭제하세요.)
2. 엘리베이터 신호 변경 (아직 테스트가 필요합니다.)

v 1.0.4
-------
1. 콘센트(Outlet) 추가 (기기 재검색이 필요합니다. /share 폴더의 기기 파일을 삭제하세요.)
2. 신호 보내는 주기를 0.2초에서 0.01초로 변경

v 1.0.3
-------
1. elfin을 통해 받은 신호가 겹치는 경우 32자리씩 잘라서 데이터를 수집합니다.
   (기기 검색시에도 잘라서 수집합니다.)

v 1.0.2
-------
1. elfin을 통해 받은 신호가 겹치는 경우 32자리씩 잘라서 데이터를 수집합니다.

v 1.0.1
-------
1. 기기 검색방법 변경
2. slug 변경
3. 재설치가 필요합니다.

v 1.0.0
-------
1. 첫 릴리즈
