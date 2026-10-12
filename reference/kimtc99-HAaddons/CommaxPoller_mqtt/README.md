# Commax RS485 Poller (MQTT)

월패드 없이 RS485 버스를 유지하기 위한 **최소 기능** 애드온.

HA 연동·상태 파싱은 담당하지 않습니다.
MQTT 브로커를 통해 EW11에 주기적으로 각 기기에 상태 조회 패킷만 발행합니다.

## 동작 방식

월패드가 없으면 RS485 버스에 마스터가 없어 기기들이 상태를 응답하지 않습니다.
이 애드온이 월패드 대신 마스터 역할을 해 버스를 활성 상태로 유지합니다.

```
[이 애드온]
    │
    │ 폴링 패킷 발행 (MQTT publish, 주기적)
    ▼
 MQTT 브로커
    │
    ▼
  EW11 (MQTT 모드) ──► RS485 버스 ──► 기기들 (조명, 난방, 가스 …)
                              ◄────────────────── 상태 응답
```

상태 응답은 EW11을 통해 MQTT 수신 토픽으로 발행되며,
다른 HA 연동 앱(Homenet2MQTT 등)이 이를 구독해 처리합니다.

## 설정

| 이름 | 기본값 | 설명 |
|:--|:--|:--|
| `mqtt_server` | — | MQTT 브로커 IP 주소 |
| `mqtt_port` | `1883` | MQTT 브로커 포트 |
| `mqtt_id` | — | MQTT 사용자명 (비어있으면 인증 없음) |
| `mqtt_password` | — | MQTT 비밀번호 |
| `mqtt_send_topic` | `ew11/send` | EW11이 구독하는 송신 토픽 |
| `poll_interval` | `5` | 전체 기기 폴링 주기 (초) |

## 기기 정보 파일

`/apps/devinfo.json`에 폴링할 기기 목록(이름, 개수, 응답 prefix)을 정의합니다.
직접 수정해서 댁의 기기 구성에 맞게 조정하세요.

## EW11 설정

EW11 관리 페이지에서 소켓을 추가해 MQTT 모드로 채널을 구성합니다.

- Protocol: **MQTT**
- Broker: 위 `mqtt_server`/`mqtt_port`와 동일하게 설정
- Subscribe Topic: `ew11/send` (이 애드온이 발행하는 토픽과 일치)
- Publish Topic: 다른 연동 애드온이 구독하는 수신 토픽 (예: `ew11/recv`)
- Route: Uart (RS485)

---

이 애드온은 Claude(Anthropic)로 작성되었습니다.
