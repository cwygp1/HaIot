# kimtc99/HAaddons 분석 (원작자의 새 저장소)

- 주소: https://github.com/kimtc99/HAaddons (끝에 **s**). 우리가 쓰던 `kimtc99/HAaddon`은 삭제됐고, 2024-04에 이 이름으로 다시 올렸다. `repository.json`의 이름은 그대로 "Saram's Repository".
- 커밋 10개. 2024-04-20~21 (CommaxWallpadBySaram), 2026-06-17~19 (CommaxPoller 3종). 스타 9.
- 로컬 복사본: `reference/kimtc99-HAaddons/` (이 저장소), 원본 클론: `../kimtc99-HAaddons/`

## 1. 구성

| 폴더 | 이름 / slug | 버전 | 날짜 | 용도 |
|---|---|---|---|---|
| `CommaxWallpadBySaram` | COMMAX Wallpad by Saram / `cwbs` | 1.0.1 | 2024-04-21 | 우리 애드온([TEST] COMMAX Wallpad (EW11) v1.2.3)의 **후속판**. EW11 MQTT 모드 |
| `CommaxPoller` | Commax RS485 Poller / `commax_poller` | 1.0.0 | 2026-06 | **월패드가 없는 집**에서 버스 마스터 역할만 하는 폴링 데몬. EW11 TCP 8899 |
| `CommaxPoller_mqtt` | … (MQTT) / `commax_poller_mqtt` | 1.0.0 | 2026-06 | 같은 기능, EW11 MQTT 모드(`ew11/send`로 publish) |
| `CommaxPoller_udp` | … (UDP) / `commax_poller_udp` | 1.0.0 | 2026-06 | 같은 기능, EW11 UDP Server 9999. TCP 세션 슬롯 경합 회피용 |

README에 "시리얼 통신 앱은 HAKorea/addons로" 라고 적혀 있지만 그 저장소는 현재 404다. CommaxPoller README 3개 모두 "이 애드온은 Claude(Anthropic)로 작성되었습니다"라고 명시.

## 2. CommaxWallpadBySaram vs 우리 v1.2.3 (diff 결과)

`cwbs_devinfo.json`은 우리 `commax_devinfo.json`과 **바이트 단위로 동일**. 패킷 정의는 바뀐 게 없다. 코드 차이는 아래가 전부다.

| 항목 | 우리 v1.2.3 (`commax-mqtt-test`) | CWBS 1.0.1 | 영향 |
|---|---|---|---|
| HA 토픽 접두사 | `commax` 고정 | 옵션 `mqtt_TOPIC` (기본 `cwbs`) | |
| HA 엔티티 ID | `commax_light1`, `commax_thermo1` … | `cwbs_light1`, `cwbs_thermo1` … | **갈아타면 엔티티 ID가 바뀜** → 대시보드/자동화 수정 필요 (옵션으로 `mqtt_TOPIC: commax`를 줘도 object_id는 `cwbs_` 고정) |
| 장치 파일 | 읽기 `commax_found_device_new.json`, 쓰기 `commax_found_device.json` (**불일치 버그** → 매 부팅 20초 재검색) | 읽기/쓰기 모두 `cwbs_found_device.json` | 버그 수정됨 |
| `save_unregistered_signal` 옵션 | 있음(동작 안 함) | 삭제 | |
| 명령 전송 조건 | HOMESTATE에 현재 상태가 **있고** 값이 다를 때만 송신. 상태를 한 번도 못 받은 장치는 명령이 무시됨("There is no command about") | 무조건 송신 | 우리 버전은 EW11 패킷을 못 받는 상황에서 HA 제어가 전혀 안 되는 구조. CWBS는 됨 |
| 상태 publish | 값이 바뀔 때만 publish (HOMESTATE 캐시) | 매 수신마다 publish (온도만 HOMESTATE 저장) | MQTT 트래픽 증가, 대신 상태 꼬임 없음 |
| paho-mqtt | 2.x (`CallbackAPIVersion.VERSION1`) — 2024-02-21 "버전업으로 인한 소스 수정"이 이것 | **1.6.1 고정** (`mqtt.Client('cwbs')`) | |
| 베이스 이미지 | python:3.8-slim-buster | python:3.10-slim-buster | telnetlib은 3.13에서 제거됐지만 3.10이라 문제 없음 |
| 로그 | 영문 혼용 | 한글. 저장 로그에 `cowbs_found_device.json` 오타 | |
| EW11 재시작 (10초 무응답 시 telnet `Restart`) | 동일 | 동일 | |
| 송신 타이밍 | 100 ms 유휴 후 송신, 5회 재시도 | 동일 | |

결론: **CWBS = 우리 애드온 + 파일명 버그 수정 + "상태 없어도 명령 송신" 수정**. 그 외 로직은 같다. 우리 복사본 `cwygp/HA`를 2024-02-21에 손본 것(paho 2.x 대응)은 CWBS에는 반영돼 있지 않고 대신 1.6.1로 고정했다.

## 3. CommaxPoller 분석 — "월패드 없이 버스 유지"

### 문제 의식
코맥스 RS485는 **월패드가 마스터**다. 월패드가 110~150 ms마다 조회 패킷을 돌리기 때문에 장치들이 상태를 뱉는다. 월패드를 떼거나 고장 나면 버스가 조용해져서 EW11에 아무것도 안 들어온다. 이 데몬이 월패드 대신 조회만 돌린다. **상태 파싱·HA 연동은 안 한다** (README: "다른 HA 연동 앱(Homenet2MQTT 등)이 이를 수신해 처리"). → 원작자 본인이 2026년 기준 HA 연동은 **Homenet2MQTT에 맡기는 구성**을 전제하고 있다.

### 조회(폴링) 패킷 생성 규칙 (`make_poll_packet`)
```
poll_prefix = state_prefix & 0x7F        # B0→30, 82→02, A0→20, 90→10, F9→79, F6→76
packet      = poll_prefix + ID + 00 00 00 00 00 + CS
Outlet(79)만 예외: 79 + ID + ID + 00 00 00 00 + CS   (ID가 byte1·byte2에 반복)
EV(23)는 폴링 안 함 (도착 시 스스로 발신)
```
우리 패킷 분석 문서의 "응답 = 조회 + 0x80" 규칙을 코드로 확인해 주는 자료다. 콘센트 조회가 `79 ID ID`인 점은 homenet2mqtt 문서(`79 ID 01/02`)와 다르다. 월패드 모델별 차이일 수 있으니 실제 캡처로 확인 필요.

### 타이밍
- 전체 기기 한 바퀴 → `poll_interval`(기본 5초) 대기. 패킷 사이 150 ms (`RESPONSE_TIMEOUT` / `PACKET_INTERVAL`). 월패드(110~150 ms 연속 순환)보다 훨씬 느슨하다. 상태 갱신이 5초 단위면 충분하다는 설계.
- TCP판은 보낸 뒤 150 ms 동안 수신 버퍼를 읽어 버리기만 한다(내용 미사용).

### 3종 전송 방식 비교

| | TCP | MQTT | UDP |
|---|---|---|---|
| EW11 설정 | TCP Server 8899 | MQTT, Subscribe `ew11/send` | Udp Server 9999 (기존 TCP 소켓 두고 **+Add**로 추가) |
| 의존성 | 없음 (asyncio) | paho-mqtt 1.6.1 | 없음 |
| 다른 브리지와 공존 | EW11 TCP 세션이 1개로 제한되면 homenet2mqtt와 **경합** | 브로커 경유라 공존 가능 | 비연결형이라 공존 가능 (이게 UDP판을 만든 이유) |
| 소소한 버그 | – | – | config 기본 9999, 코드 fallback 8898 (옵션 저장되면 무관) |

### 우리 상황과의 관계
- 우리 집은 월패드가 있으므로 CommaxPoller 자체는 필요 없다. 다만 `commax_found_device.json`이 전부 `Number: 0`인 건 "월패드가 조회를 안 돌리거나 EW11 MQTT가 안 올라오는" 상황을 뜻하므로, 원인 확인용으로 MQTT판을 잠깐 돌려 `ew11/recv`에 응답이 생기는지 보는 용도로는 쓸 수 있다.
- homenet2mqtt 갤러리의 `heaters_request.yaml`(5초마다 `02 ID` 송신)과 같은 아이디어다.

## 4. 적용 권장

1. **애드온 스토어 저장소 주소 교체**: `https://github.com/cwygp/HA` → `https://github.com/kimtc99/HAaddons` 추가 (기존 것은 지우지 말 것. 지우면 설치된 [TEST] 애드온이 "available: false"가 될 수 있음).
2. kimtc99 계열을 계속 쓸 거면 **CWBS 1.0.1로 교체**가 낫다 (파일명 버그·명령 무시 버그 해결). 엔티티 ID가 `cwbs_*`로 바뀌는 것만 감수.
3. 장기적으로는 원작자도 전제하는 **homenet2mqtt** 구성이 합리적. 그 경우 EW11에 TCP Server 소켓을 추가하고, kimtc99 애드온은 중지.
