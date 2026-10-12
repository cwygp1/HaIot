# 현관 방문자 사진을 NAS로 가져오기 — jhkwon19 방식 재구성 계획

전제: 월패드는 안드로이드 기반, NAS가 현관 네트워크 단자함에 있음. 월패드 메인 보드는 수리 후.

## 1. jhkwon19 통합이 실제로 요구하는 것 (소스 기준)

HA 통합(`commax_wallpad`, HACS)은 **SSH로 어떤 리눅스 박스에 붙어 아래 4개 스크립트를 실행**할 뿐이다. 그 박스가 OpenWrt 공유기일 필요는 없다. NAS의 Docker 컨테이너여도 된다.

| 스크립트 (박스의 /root/) | 통합이 기대하는 출력 | 내부 동작(원작) |
|---|---|---|
| `wallpad-visitor-query.sh` | `filename=…` `uid=…` `captured_at=YYYY-MM-DDTHH:MM:SS` `flags=…` `marker=…` 5줄 | ADB로 월패드 저장소의 최신 JPEG 1장 메타 |
| `wallpad-visitor-list.sh` | JPEG 파일명 줄 단위 | ADB `ls` |
| `wallpad-visitor-image.sh <filename>` | JPEG 바이너리(stdout), 실패 시 exit≠0 | ADB `pull`/`cat` |
| `wallpad-visitor-call-monitor.sh` | 장기 실행. `ready\t…\t…`, `ringing\t<place>\t<displayname>…`, `ended\t…`, `metering\t…\t<sort>\t<json>` | ADB `logcat` 스트림 파싱 |

파일명 규칙: `<MAC12hex>-<동>-<호>-<uid hex>-<YYYY-MM-DD>-<HH-MM-SS>-<flags…>.jpg`, flags 세 번째 값 0=세대현관, 1=공동현관.
호출 place: 2=세대현관, 5=공동현관. SIP DisplayName(lobby0105 등)으로 공동현관 위치 세분화.
엘리베이터는 단지 서버 HTTP `getEvStatus`(옵션). L2 MITM 브리지는 이 HTTP를 월패드 명의로 보내기 위한 장치이지, 사진·호출에는 필요 없다.

핵심 의존성은 하나다: **월패드에 ADB로 들어갈 수 있는가.**

## 2. ADB 경로 두 가지

| | TCP ADB (포트 5555) | USB ADB |
|---|---|---|
| 조건 | 월패드가 `service.adb.tcp.port=5555`로 떠 있거나 루팅돼 있음 | 월패드 USB 포트(보통 하단/후면 서비스 포트)에 케이블 |
| 박스 위치 | **NAS(단자함)에서 바로** 가능 | USB 5 m 한계 → 월패드 옆에 라즈베리파이 제로/OpenWrt 같은 소형 박스 필요. 원작이 공유기를 쓴 이유 |
| 확인법 | 월패드 세그먼트에서 `nmap -p 5555 <월패드IP>` → open이면 `adb connect IP:5555` | `adb devices`에 잡히는지 |

## 3. 네트워크 주의점 (단자함이라서 생기는 문제)

- 월패드 이더넷은 보통 **단지망**(관리사무소 서버·공동현관기와 같은 세그먼트)이고, 집 안 LAN(NAS가 있는 망)과 **다른 망**이다. 단자함에 두 선이 다 들어오지만 서로 연결돼 있지 않다.
- 따라서 NAS가 월패드와 통신하려면 **월패드 쪽 세그먼트에 NIC 하나를 물려야** 한다. NAS 2번 LAN 포트(또는 USB 이더넷 어댑터)를, 단지망 인입선과 월패드 사이에 끼운 작은 스위치에 연결한다.
- 그 NIC에 IP를 주면 단지망에 "낯선 장비"가 하나 생기는 셈이다. 관리 서버가 MAC/IP를 감시하는 단지도 있다. 원작이 IP 없는 순수 L2 브리지로 만든 이유. 처음엔 **IP 없이 tcpdump만**(수동 관찰)으로 시작하고, ADB 연결이 필요해지면 그때 월패드 서브넷의 안 쓰는 IP를 수동으로 준다.
- 미러링 되는 관리형 스위치가 있으면 더 깔끔하다(NAS NIC는 미러 포트에만).

## 4. 단계

1. **월패드 수리** (메인 보드). 이게 없으면 전부 불가.
2. 단자함: 단지망 인입선 → 소형 스위치 → 월패드. 스위치의 다른 포트 → NAS 2번 NIC. NIC는 IP 없이.
3. NAS에서 수동 관찰 (Docker, `--net=host`, `NET_ADMIN`):
   ```bash
   tcpdump -i eth1 -nn -e not arp | head -200      # 월패드 IP/MAC, DHCP, SIP 서버, HTTP 서버 파악
   tcpdump -i eth1 -nn port 5060 or portrange 5004-5010   # 벨 눌러보며 SIP/RTP 흐름 확인
   ```
4. 월패드 IP를 알면 임시 IP를 NIC에 주고 `nmap -p 5555,5037,22,8080 <월패드IP>`.
   - 5555 열림 → `adb connect` → `adb shell` 에서 사진 경로 탐색: `find /sdcard /data -iname '*.jpg' -mmin -1440 2>/dev/null`, `logcat -v time | grep -i -E 'sip|ring|invite|visitor|door'` 로 호출 로그 태그 확인.
   - 닫힘 → 월패드 USB 포트 확인 후 월패드 옆 소형 박스 경로로 전환.
5. 위에서 확인한 경로·로그 태그로 4개 스크립트를 작성해 NAS 컨테이너(alpine + android-tools + openssh)의 `/root/`에 두고, HA에 jhkwon19 통합을 HACS로 설치 → 호스트=NAS IP, SSH 포트/계정 입력.
6. 엘리베이터 HTTP는 선택. RS485(A0 호출 패킷)로 이미 되므로 생략 가능.

## 5. 지금 할 수 있는 것 / 없는 것
- 지금: 2번(배선)과 3번(관찰)은 월패드가 죽어 있어도 준비는 가능하지만 트래픽이 없어 확인은 안 된다.
- 스크립트 4개는 사진 경로와 logcat 태그를 알아야 쓸 수 있다. 4단계 결과가 나오면 작성한다.
- 월패드 모델명이 있으면 ADB TCP 기본 활성 여부와 USB 포트 위치를 미리 조사할 수 있다.

## 6. ADB 없이 가능한 경로 (전부 "패킷 관찰 결과"에 달림)

월패드가 만든 JPEG를 꺼내는 게 ADB다. ADB를 안 쓰면 **사진을 우리가 직접 만들거나, 월패드 말고 다른 곳에서 받아야** 한다.

| 경로 | 조건 | 얻는 것 | 난이도 |
|---|---|---|---|
| A. RTP 영상 수동 캡처 | IP 도어폰(SIP/RTP), 영상 비암호화(SRTP 아님), 단자함에 미러링 스위치 또는 NAS 2-NIC 브리지 | 벨 울림 이벤트(SIP INVITE) + 통화 영상에서 뽑은 스냅샷 | 중. tcpdump로 RTP 잡아 ffmpeg(H.264)로 프레임 추출. 공동현관기는 보통 호출 즉시 영상을 보냄(early media)이라 월패드가 받지 않아도 흐를 가능성 높음 |
| B. 단지 서버 HTTP 재현 | 월패드가 "방문자 확인" 메뉴에서 단지 서버의 HTTP로 사진 목록/이미지를 받아오는 구조일 때 | 서버에 저장된 방문자 JPEG(파일명에 MAC-동-호가 있는 걸 보면 서버 저장 가능성 큼) | 중. 캡처로 URL/인증을 알아낸 뒤 NAS에서 같은 요청. 서버가 월패드 IP/MAC을 검사하면 L2 브리지에서 월패드 주소로 위장 필요(원작의 getEvStatus와 같은 수법) |
| C. 공동현관기 직접 접근 | 공동현관기/세대현관 카메라가 RTSP나 HTTP 스냅샷을 제공할 때 | 실시간 영상/스냅샷 | 하~중. 캡처에서 장비 IP·포트·인증을 확인 |
| D. 루비에 클라우드 API | 루비에 앱에 "방문자 사진"이 뜨는 단지일 때 (월패드가 클라우드에 업로드) | 앱이 보는 것과 같은 사진 | 중~상. 앱 API 리버싱. 월패드가 살아 있어야 함 |

주의
- A·B·C 모두 **월패드 메인 보드와 무관하게** 동작할 수 있는 건 A와 C뿐이다(B·D는 월패드가 사진을 만들어 올려야 함). 다만 A도 월패드가 SIP 등록이 안 돼 있으면 공동현관기가 호출 자체를 안 걸 수 있다.
- NAS를 인라인 브리지로 두면 NAS가 재부팅되는 동안 월패드 네트워크(도어폰!)가 끊긴다. **미러링 되는 관리형 스위치**가 더 안전하다.
- ADB 경로가 더 쉬운 이유: 월패드가 이미 만든 JPEG와 호출 로그를 그대로 가져오기만 하면 되고, jhkwon19 통합을 그대로 쓸 수 있다. ADB 없는 경로는 HA 쪽 엔티티(image/event/MQTT)를 직접 만들어야 한다.
- 결정은 3단계(tcpdump 관찰) 뒤에. 관찰 없이는 어느 경로가 열려 있는지 알 수 없다.
