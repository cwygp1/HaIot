# [TEST] COMMAX Wallpad (EW11) v1.2.4

kimtc99의 `commax-mqtt-test` v1.2.3을 바탕으로 제어 불량 원인(저장소 루트 `제어불량_원인분석.md`)을 고친 버전.
EW11을 **MQTT 모드**로 두고, 이 애드온이 브로커를 통해 패킷을 주고받는다.

## EW11 설정
- Serial: 9600 / 8 / None / 1
- Communication Settings: Protocol **MQTT**, Broker = HA의 Mosquitto IP:1883, 계정은 애드온 옵션과 동일
  - Publish Topic: `ew11/recv`
  - Subscribe Topic: `ew11/send`
- telnet 켜기(워치독 재시작용, 선택)

## 설치
1. 애드온 스토어 → 저장소 추가: `https://github.com/cwygp1/HaIot` (저장소가 public이어야 supervisor가 받을 수 있음)
2. "[TEST] COMMAX Wallpad (EW11)" 설치 → 구성 입력 → 시작
3. 첫 실행 시 `/share/commax_found_device.json`이 없으면 `scan_seconds`(20초) 동안 패킷을 들어 장치 수를 센다.
   - 하나도 못 찾으면 저장하지 않고 30초마다 재검색 (EW11 전원·MQTT 설정 확인)
   - 찾은 뒤 개수가 실제와 다르면 파일의 `Number`를 손으로 고치고 애드온 재시작
   - v1.2.3 때 쓰던 `commax_found_device_new.json`이 있으면 그 파일을 우선 사용

## 옵션
| 옵션 | 기본 | 설명 |
|---|---|---|
| mqtt_server / mqtt_port / mqtt_id / mqtt_password | | 브로커 접속 정보 |
| elfin_server / elfin_id / elfin_password | | EW11 IP와 telnet 계정 (비우면 재시작 안 함) |
| send_gap_ms | 15 | 마지막 수신 후 이만큼 조용해야 송신 (코맥스 버스 주기 ~108ms, 100ms로 두면 거의 못 보냄) |
| retry_interval_ms | 300 | 응답 없을 때 재전송 간격 |
| max_retry | 5 | 재전송 횟수. 초과 시 WARNING 로그 후 폐기 |
| scan_seconds | 20 | 최초 장치 검색 시간 |
| ew11_restart_after_sec | 60 | 이 시간 무신호면 EW11 telnet 재시작 |
| ew11_restart_min_interval_sec | 300 | 재시작 최소 간격 |
| DEBUG / mqtt_log / elfin_log | | 로그 수준 (elfin_log는 raw 패킷까지) |

## HA 엔티티
MQTT Discovery로 자동 등록. unique_id는 v1.2.3과 동일(`commax_light1` 등)이라 기존 엔티티가 그대로 이어진다.
- light.commax_lightN, switch.commax_lightbreaker1, climate.commax_thermoN (off/heat, 5~40℃)
- fan.commax_fan1 (preset low/medium/high), switch.commax_outletN + sensor.commax_outletN_watt
- button.commax_gas1 (잠금), button.commax_ev1 (호출) + sensor.commax_ev1_floor
- 가용성: `commax/status` (애드온 종료 시 unavailable)

## 상태/명령 토픽
- 상태: `commax/{Device}{N}/{power|setTemp|curTemp|speed|watt|floor}/state` (retain)
- 명령: `commax/{Device}{N}/{power|setTemp|speed}/command`

## 로컬 테스트
`python3 -m venv v && v/bin/pip install "paho-mqtt>=2,<3"` 후 저장소의 테스트 스크립트(원인분석 문서 참조)로 가짜 패킷을 흘려 검증할 수 있다.

## 버스 충돌(패킷 겹침)과 송신 타이밍

코맥스 홈넷은 Modbus가 아니다(주소/기능코드/CRC16 없음, 8바이트 고정 + 합산 체크섬). 하지만 같은 **RS485 반이중 멀티드롭** 버스라
두 송신기가 동시에 드라이브하면 두 프레임 모두 깨진다. 전기적으로는 일반 RS485 트랜시버가 단락 보호돼 있어 짧은 충돌은 무해하고,
깨진 프레임은 체크섬 불일치로 모두가 버린다. 결과는 "월패드가 그 주기 한 번 상태를 못 받음 + 내 명령이 씹힘"이며, 명령은
`retry_interval_ms` 뒤 재전송된다. 즉 충돌은 **치명적이지 않지만 제어 성공률을 직접 깎는다.**

버스 1주기(월패드 기준, 9600bps 8바이트 = 8.3 ms):
```
t=0      조회 (월패드→장치)        8 ms
t≈15~25  응답 (장치→월패드)        8 ms
t≈25~108 유휴                      ← 여기에 끼워 넣어야 함
t≈108    다음 조회
```

문제는 이 애드온이 버스를 **직접** 보지 못한다는 점이다. EW11은 `Gap Time`(기본 수십 ms)만큼 조용해져야 프레임을 끊어 보내고,
그 뒤 Wi-Fi·브로커·애드온·브로커·EW11을 거쳐 버스에 실린다. 따라서 애드온이 측정하는 "마지막 수신 후 N ms"는 실제 버스 기준으로
`N + GapTime + 왕복 네트워크 지연(10~40 ms)` 뒤다.

| 설정 | 버스에 실리는 시점(대략) | 평가 |
|---|---|---|
| v1.2.3: 100 ms | 응답 후 100+50+20 ≈ 170 ms → 다음 주기의 60 ms 지점 | 유휴 구간이긴 하지만 거의 매 주기 창을 놓침(원인분석 #3) |
| v1.2.4: 15 ms, EW11 Gap 50 ms | 응답 후 15+50+20 ≈ 85 ms → 다음 조회(108) 직전 | 네트워크가 느리면 다음 조회와 **충돌 가능** |
| v1.2.4: 15 ms, EW11 Gap 10 ms | 응답 후 15+10+20 ≈ 45 ms → 유휴 중앙 | 권장 |

권장 설정
1. EW11 Serial Port Settings의 **Gap Time을 10 ms**(최소값)로 낮춘다. 조회/응답이 따로 올라오게 되며 지연이 줄어든다.
2. `poll_wait_ms`(기본 40): 마지막 패킷이 **조회**(헤더 < 0x80)였으면 응답이 끝날 때까지 더 기다린다. 응답(헤더 ≥ 0x80)을 본 뒤에는 `send_gap_ms`만 기다린다.
3. `elfin_log: true`로 켜고 명령을 10번 보내 `응답 없음 … 폐기` 경고 비율을 본다. 폐기가 잦으면 `send_gap_ms`를 5 ms 단위로 올리거나 내려 가장 성공률 높은 값을 찾는다.
4. 재시도(`retry_interval_ms` 300 ≈ 3주기)가 충돌 복구 수단이다. 0으로 만들지 말 것.
5. 지연을 근본적으로 줄이려면 EW11을 TCP Server 모드로 두고 homenet2mqtt처럼 소켓 직결하거나(브로커 홉 제거), ESP32를 버스에 직접 붙이는 방법(eigger)이 있다.
