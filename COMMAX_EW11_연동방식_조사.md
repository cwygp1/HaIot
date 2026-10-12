# COMMAX 월패드 × EW11 연동 방식 조사 (2026-10-12)

웹 공개 자료를 모아 정리한 것이다. 이 저장소의 `COMMAX_패킷구조_분석.md`(우리 애드온 소스 분석)와 교차 검증했다.

## 0. 한 줄 요약

- **EW11 하드웨어 연결 방법은 하나**다: RS485 A/B 선에 물리고, 9600 8N1로 맞추고, Wi-Fi로 패킷을 내보낸다.
- **소프트웨어는 EW11의 통신 모드에 따라 두 갈래**다.
  - **MQTT 모드**: EW11이 직접 MQTT 브로커에 publish/subscribe → 우리 애드온([TEST] COMMAX Wallpad (EW11), kimtc99 제작)이 이 방식. 원작자 저장소는 삭제됨.
  - **TCP 서버 모드(포트 8899 등)**: 브리지 프로그램이 EW11에 소켓 접속 → **homenet2mqtt(h2m)** 가 현재 가장 활발히 유지되는 선택지(2026-09 기준 업데이트 중, 코맥스 전용 갤러리 설정 내장).
- 코맥스 패킷 구조는 모든 자료가 일치한다. 8바이트 고정, 합산 체크섬, `[HDR][ID][값]`(명령) / `[HDR][값][ID]`(상태), 응답 HDR = 요청 HDR + 0x80.
- 집마다 다른 부분은 **가스밸브 상태값, 일괄소등 byte5, 엘리베이터 층 알림 헤더(0x23/0x26)** 뿐이다. 이건 직접 캡처해서 맞춰야 한다.

---

## 1. EW11 하드웨어 측 (공통)

| 항목 | 내용 | 출처 |
|---|---|---|
| 모델 | Elfin **EW11** (RS485↔Wi-Fi). EW10은 RS232용이라 쓰면 안 됨. EW11A는 외장 안테나형 | oriyaho, tech.yangs |
| 전원 | 5~36V DC. 월패드 뒤 12V 라인 또는 5V USB 어댑터. **극성 반대로 꽂으면 고장** | velog(minu-j), oriyaho |
| 전원 주의 | 월패드 RJ45 라인에서 전원을 끌면 EW11 부하로 4V 이하로 떨어지는 사례 있음 → 방범/도어폰 쪽 12V 별도 인출 | oriyaho #45 |
| 신호선 | RS485 A(+)/B(−) 두 가닥. 월패드 뒷면 RS485 포트, 또는 서브폰/주방TV RJ45의 4·5번 핀에서 분기(모델별 다름) | oriyaho #45, velog |
| 분기 | 월패드 RS485 포트가 부족하면 RJ45 커플러(Y분기)로 병렬 연결. RS485는 멀티드롭이라 병렬로 붙여도 됨 | velog |
| 초기 접속 | EW11이 `EW11_xxxx` AP를 띄움 → `10.10.100.254` (admin/admin) 접속 → STA 모드로 집 2.4GHz Wi-Fi 등록, 공유기에서 **고정 IP** 부여 | tech.yangs, oriyaho #22 |
| Serial Port Settings | **9600 bps, 8 data, None parity, 1 stop** (코맥스). 다른 제조사(삼성SDS 등)는 even parity | h2m requirements.json, tech.yangs |
| Communication Settings | 아래 2안 중 택1 (둘 다 등록해 두는 것도 가능) | |

### 통신 모드 2안

| | A. MQTT 모드 | B. TCP Server 모드 |
|---|---|---|
| EW11 설정 | Protocol=MQTT, 브로커 IP/계정, publish topic `ew11/recv`, subscribe topic `ew11/send` (우리 애드온 기준 토픽명) | Protocol=TCP Server, Local Port 8899(관례) |
| 소프트웨어 | kimtc99 [TEST] COMMAX Wallpad (EW11) 애드온 = **현재 우리 HA** | homenet2mqtt, HAKorea Universal(중단), 윈도우 시리얼 모니터 등 |
| 장점 | 브리지 프로그램이 죽어도 EW11이 브로커에 바로 쏨. 패킷을 MQTT로 바로 구경 가능 | 거의 모든 브리지 프로그램이 지원. h2m 웹 UI 패킷 모니터 사용 가능 |
| 단점 | 지원 프로그램이 kimtc99 계열뿐 | TCP 세션 1개만 허용되는 경우가 있어 모니터링과 브리지를 동시에 못 붙일 수 있음 |

현재 우리 EW11 IP는 `192.168.1.3`(애드온 옵션 `elfin_server`), MQTT 브로커는 `192.168.1.28`.

---

## 2. 소프트웨어 선택지 비교

| 프로젝트 | 상태 (2026-10) | EW11 연결 | 특징 | 비고 |
|---|---|---|---|---|
| **kimtc99/HAaddon → cwygp/HA 복사본** `[TEST] COMMAX Wallpad (EW11)` v1.2.3 | 원본 삭제. 복사본만 생존 (이 저장소) | MQTT 모드 | Python, 20초 자동 장치검색, MQTT Discovery, 10초 무응답 시 EW11 telnet 재시작 | 현재 사용 중. `_new.json` 파일명 버그로 매번 재검색 가능성 |
| **kimtc99/HAaddons** (원작자 새 저장소) `CommaxWallpadBySaram` 1.0.1 + `CommaxPoller` 3종 | 2024-04 재작성, 2026-06 Poller 추가 | MQTT 모드 (Poller는 TCP/MQTT/UDP 택1) | 우리 애드온의 후속판(버그 2개 수정, 패킷 정의 동일). Poller는 월패드 없는 집용 버스 마스터 데몬 | 상세: `kimtc99_HAaddons_분석.md` |
| zooil/commaxRS485 (그레고리하우스 nodejs 포팅) | 2020년 종료, deprecated | 시리얼/소켓 | `/share/commax_serial_wallpad.js`가 이 계열 | HAKorea Universal로 넘어가라고 안내 |
| HAKorea/addons `wallpad` (Universal Wallpad Controller) | **저장소 자체가 404** (삭제됨) | TCP 소켓 | 코맥스/삼성/대림/현대 공용 nodejs | 더 이상 선택지 아님 |
| **wooooooooooook/homenet2mqtt (h2m)** | 활발 (2026-09 push, 이슈 #700대) | TCP Server (`path: IP:8899`) | HA 애드온(`https://github.com/wooooooooooook/HAaddons`), 웹 UI 패킷 모니터, YAML 규칙, **코맥스 갤러리**(조명/난방/일괄소등/가스/콘센트/환기/엘리베이터/가상조명), MQTT Discovery, Matter 모드 | EW11 유지하면서 갈아탈 수 있는 1순위 |
| eigger/espcomponents `uartex` + `packages/wallpad/commax` | 활발 (2026-10 push, ★94) | **EW11 아님** (ESP32+RS485 모듈) | ESPHome. 패킷 정의가 h2m과 동일 | EW11 버리고 ESP32로 갈 때의 선택지. 패킷 레퍼런스로만 활용 |
| jhkwon19/commax_wallpad | 2026-09, HACS custom component | 불명 ("MITM") | 방문자/통화 기능 중심, 소스는 비공개 미러 | 정보 부족 |
| adminkcs/commax-ew11-edge | 저장소 404 | EW11 TCP | SmartThings Edge 드라이버 | 검색 결과 요약만 남음(8바이트 프레임, 9600 8N1) |

---

## 3. 패킷 구조 교차 검증 결과

세 독립 출처(우리 애드온 소스, h2m `docs/manufacturers/commax.md`, eigger `commax/README.md`)가 모두 같은 구조를 기술한다. 아래는 세 출처를 합친 **최종 표**다. (ID = 장치 번호 1~, CS = 앞 7바이트 합 & 0xFF)

### 3.1 공통 규칙
- 8바이트 고정, 헤더/푸터 없음, 체크섬 = `sum(byte0..6) & 0xFF` (h2m `rx_checksum: add`).
- 월패드 → 장치: **조회** `[HDR_Q][ID][00…]`, **명령** `[HDR_C][ID][서브명령/값]…`
- 장치 → 월패드: **상태** `[HDR_Q+0x80][값][ID]…`, **명령 응답(ack)** `[HDR_C+0x80][값][ID]…`
- 온도·전력량·층수는 **BCD** (0x23 = 23).

### 3.2 헤더 표

| 장치 | 조회(월패드→) | 상태(장치→) | 명령(월패드→) | 명령응답(장치→) |
|---|---|---|---|---|
| 조명 | 30 ID | B0 on ID | 31 ID on | B1 on ID |
| 일괄소등 | 20 ID | A0 on ID 00 00 **15/17** 00 | 22 ID on 01 | A2 on ID … |
| 난방 | 02 ID | 82 st ID cur set | 04 ID 04 81/00 (전원) / 04 ID 03 BCD (온도) | 84 st ID cur set |
| 가스 | 10 ID | 90 s s | 11 ID 80 (잠금만) | 91 … |
| 콘센트 | 79 ID 01(전력)/02(에코) | F9 pwr ID 11 D1 D2 D3 | 7A ID 01(전원)/02(에코)/03(차단값) val | FA … |
| 환기 | 76 ID | F6 st ID spd | 78 ID 01(전원 00/04) / 78 ID 02(풍량 1~3) | F8 … |
| 엘리베이터 | – | **23** 01 floor / **26** 01 01 46 00 floor 02 (변형) | A0 01 01 00 08 15 00 (일괄소등 상태 헤더를 차용) | 23 … |

### 3.3 값 정의
- 난방 `st`: 0x80 OFF, 0x81 ON(대기), **0x83 ON(가열 중)**. h2m은 `mask 0xF9`로 0x80/0x82 계열을 모두 같은 장치로 잡음.
- 환기 `st`: 0x00 OFF, 0x04 수동(약/중/강), 0x02 자동, 0x06 취침. `spd` 1/2/3.
- 콘센트 `pwr`: 0x00 OFF, 0x01 ON, 0x10 OFF+대기전력차단, 0x11 ON+차단. 전력 = BCD(D1 D2 D3) × 0.1 W. `TYPE 0x21`은 에코모드 보고(무시).
- 가스 `s s`: 집마다 다름. 관측된 조합 → 열림 `A0 A0`/`80 80`/`40 40`(h2m은 40=닫힘), 잠김 `50 50`/`48 48`/`40 40`, 잠금 응답 `91 48 48`/`91 58 58`/`91 88 88`. **반드시 직접 캡처.**
- 일괄소등 byte5: 0x15(우리 집·kimtc99) vs 0x17(h2m/eigger 예제). 모델 차이. v1.2.x는 16자리 완전일치라 영향이 있었고, v1.3.0부터 [헤더][값][ID] 마스크 매칭이라 영향 없음.
- 엘리베이터 층 알림: 0x23 헤더면 byte2가 층, 0x26 헤더면 byte5가 층(BCD). 호출 패킷은 두 경우 모두 `A0 01 01 00 08 15 00 BF`.

### 3.4 타이밍 / ack (카페 글 "코맥스는 ack패킷이 없는거죠..?" + 소스)
- 월패드가 장치들을 **약 110~150 ms 간격으로 순환 조회**한다. 예: `02 01 00 00 00 00 00 03` → 약 10~20 ms 뒤 `82 81 01 23 23 00 00 4A`.
- **조회→상태 응답에는 별도 ack가 없다.** 상태 패킷 자체가 응답이다.
- **명령에는 ack가 있다.** 헤더 +0x80 패킷(B1/84/A2/91/FA/F8)이 돌아오면 성공. 우리 애드온·h2m·eigger 모두 이 패킷을 `ack`로 매칭해 재전송을 멈춘다.
- 송신 타이밍 권장값

| 구현 | 버스 유휴 대기 | ack 타임아웃 | 재시도 |
|---|---|---|---|
| kimtc99 (우리) | 마지막 수신 후 100 ms | – | 5회 |
| h2m 기본값 | `tx_delay` 10 ms, `rx_timeout` 10 ms | 200 ms | 5회 |
| eigger | 50 ms | 500 ms | 3회 |

- 월패드가 난방 조회를 안 하는 집은 h2m 갤러리의 `heaters_request.yaml`(5초마다 `02 ID …` 직접 송신)로 대체 가능.
- 월패드에 조명이 연동되지 않은 집은 `virtual_lights.yaml`로 월패드 조회(0x30)에 가짜 응답(0xB0)을 줘서 HA 조명을 월패드에 "등록"시킬 수 있다.

---

## 4. 우리 집 적용 시나리오

### A. 지금 애드온 그대로 쓰기 (최소 변경)
1. `/share/commax_found_device.json`을 `/share/commax_found_device_new.json`으로 복사(또는 소스의 파일명 수정) → 부팅마다 20초 재검색 중단.
2. 검색 결과가 전부 `Number: 0`이므로 EW11 MQTT 모드가 실제로 `ew11/recv`에 패킷을 올리는지 먼저 확인 (MQTT Explorer 또는 `mosquitto_sub -t 'ew11/#'`).
3. 패킷이 보이면 json의 `Number`와 가스/일괄소등 값을 손으로 맞춘다.

### B. homenet2mqtt로 이전 (권장, EW11 유지)
1. EW11 웹 설정 → Communication Settings에 **TCP Server, port 8899** 항목 추가(기존 MQTT 항목은 남겨도 됨. 세션 수 제한은 EW11 설정의 max 접속 수 확인).
2. HA 애드온 저장소 `https://github.com/wooooooooooook/HAaddons` 추가 → `homenet2mqtt` 설치.
3. 초기 마법사: 예제 설정 **commax** 선택, 시리얼 경로 `192.168.1.3:8899`, 9600 8N1.
4. 웹 UI 패킷 모니터로 실제 패킷 확인 → 갤러리에서 `lights_new`, `heaters_new`, `light_breaker`, `gas_valve`, `smart_plugs_new`, `fan_new`, `elevator`(또는 `elevator_26`) 선택, 개수 입력.
5. 가스/일괄소등 값이 예제와 다르면 YAML의 `state_open/closed` 바이트만 수정.
6. 기존 애드온은 중지(둘이 동시에 명령을 보내면 버스 충돌).

### C. ESP32로 교체 (EW11 버림)
- M5Stack Atom Lite + Atomic RS485 Base(약 2만 원대) + eigger `commax.yaml`. Wi-Fi 끊김에 강하고 지연이 적지만 하드웨어 교체가 필요. 패킷 정의는 B와 동일.

---

## 5. 참고 자료

- homenet2mqtt 저장소: https://github.com/wooooooooooook/homenet2mqtt — 코맥스 문서 `docs/manufacturers/commax.md`, 갤러리 `gallery/commax/*.yaml`, 예제 `packages/core/config/examples/commax.homenet_bridge.yaml`
- homenet2mqtt 문서: https://homenet2mqtt-docs.vercel.app (애드온 설치, serial 설정, 트러블슈팅)
- eigger/espcomponents 코맥스 패키지: https://github.com/eigger/espcomponents/tree/master/packages/wallpad/commax
- zooil/commaxRS485 (nodejs 원형): https://github.com/zooil/commaxRS485
- EW11 설치·설정 블로그: https://blog.oriang.net/22 , https://blog.oriang.net/45 , https://velog.io/@minu-j/IoT-EW11%EB%A1%9C-%EC%9B%94%ED%8C%A8%EB%93%9C-%ED%8C%A8%ED%82%B7%EC%9D%84-%EB%B0%9B%EC%95%84%EB%B3%B4%EC%9E%90 , https://tech.yangs.kr/29
- 패킷 분석 일반론: https://jihunroh.github.io/2020/10/03/%EC%9B%94%ED%8C%A8%EB%93%9C-%EC%9B%90%EA%B2%A9%EC%A0%9C%EC%96%B4%EB%A5%BC-%EC%9C%84%ED%95%9C-RS485-%ED%8C%A8%ED%82%B7-%EB%B6%84%EC%84%9D-%EC%A4%80%EB%B9%84/
- 주의: mscg.kr/65·66, djjproject, hacollector, GuGu927/RS485 등은 **코콤(Kocom, AA55…0D0D 21바이트)** 자료라 코맥스에 적용 불가.
- 네이버 카페(HA 사용자 모임, cafe 29860180) "코맥스는 ack패킷이 없는거죠..?" 글: 조회 주기 110~150 ms, `02 01 … 03 / 82 81 01 23 23 00 00 4A` 로그 (사용자 제공 캡처)
