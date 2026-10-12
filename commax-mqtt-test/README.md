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
