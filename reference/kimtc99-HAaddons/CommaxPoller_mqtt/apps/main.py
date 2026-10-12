#!/usr/bin/env python3
"""
Commax RS485 Poller (MQTT)

월패드 없이 RS485 버스를 유지하기 위한 최소 기능 데몬 (MQTT 버전).
EW11(MQTT 모드)에 주기적으로 각 기기 상태 조회 패킷을 발행한다.
응답 파싱·상태 저장·HA 연동은 담당하지 않는다 (다른 애드온이 버스를 수신해 처리).
"""

import json
import time
import paho.mqtt.client as mqtt

DATA_DIR  = '/data'
APPS_DIR  = '/apps'

PACKET_INTERVAL     = 0.15   # 패킷 간 발행 간격 (RS485 반이중 버스 안정화용)
SKIP_POLL_PREFIXES  = {'23'}  # EV는 도착 시 자체 신호 발신, 폴링 불필요


def log(msg):
    ts = time.strftime('%Y-%m-%d %p %I:%M:%S', time.localtime())
    print(f'[{ts}] {msg}', flush=True)


def checksum(input_hex):
    try:
        input_hex = input_hex[:14]
        s1 = sum(int(input_hex[i], 16) for i in range(0, 14, 2))
        s2 = sum(int(input_hex[i], 16) for i in range(1, 14, 2))
        s1 = (s1 + s2 // 16) % 16
        s2 = s2 % 16
        return input_hex + format(s1, 'X') + format(s2, 'X')
    except Exception:
        return None


def make_poll_packet(resp_prefix, device_num):
    """RS485 상태 조회 패킷 생성

    resp_prefix : 응답 prefix (예: 'B0')
    device_num  : 1-indexed 기기 번호

    폴링 prefix = resp_prefix & 0x7F  (예: B0→30, 82→02, F9→79 …)
    Outlet(79)은 기기 번호가 2·3번째 바이트에 모두 들어감.
    """
    poll_prefix = format(int(resp_prefix, 16) & 0x7F, '02X')
    n = format(device_num, '02X')
    base = poll_prefix + n + n + '00000000' if poll_prefix == '79' \
           else poll_prefix + n + '0000000000'
    return checksum(base)


def build_poll_packets(device_list):
    """device_list에서 폴링 패킷 목록 생성"""
    packets = []
    for name, dev in device_list.items():
        num     = dev.get('Number', 0)
        resp_px = dev.get('prefix', '')
        if num > 0 and resp_px and resp_px not in SKIP_POLL_PREFIXES:
            for i in range(1, num + 1):
                pkt = make_poll_packet(resp_px, i)
                if pkt:
                    packets.append((name, i, pkt))
    return packets


def main():
    log('Commax RS485 Poller (MQTT) 시작')

    with open(f'{DATA_DIR}/options.json') as f:
        config = json.load(f)

    device_path = f'{APPS_DIR}/devinfo.json'
    with open(device_path) as f:
        device_list = json.load(f)
    log(f'기기 정보 로드: {device_path}')

    poll_packets = build_poll_packets(device_list)
    if not poll_packets:
        log('폴링할 기기가 없습니다. 기기 정보 파일을 확인하세요.')
        raise SystemExit(1)

    log('폴링 패킷 목록:')
    for name, idx, pkt in poll_packets:
        log(f'  {name}{idx} → {pkt}')

    mqtt_server = config['mqtt_server']
    mqtt_port   = config.get('mqtt_port', 1883)
    mqtt_id     = config.get('mqtt_id', '')
    mqtt_pw     = config.get('mqtt_password', '')
    send_topic  = config.get('mqtt_send_topic', 'ew11/send')
    interval    = config.get('poll_interval', 5)

    state = {'connected': False}

    def on_connect(client, userdata, flags, rc):
        if rc == 0:
            state['connected'] = True
            log(f'[MQTT] 브로커 접속 완료 ({mqtt_server}:{mqtt_port}), topic={send_topic}')
        else:
            log(f'[MQTT] 접속 실패 (rc={rc})')

    def on_disconnect(client, userdata, rc):
        state['connected'] = False
        log(f'[MQTT] 연결 끊김 (rc={rc}). 재접속 시도...')

    client = mqtt.Client('commax_poller_mqtt')
    if mqtt_id:
        client.username_pw_set(mqtt_id, mqtt_pw)
    client.on_connect    = on_connect
    client.on_disconnect = on_disconnect
    client.connect_async(mqtt_server, mqtt_port)
    client.loop_start()

    log(f'폴링 루프 시작 (주기 {interval}s)')

    while True:
        if state['connected']:
            for name, idx, pkt in poll_packets:
                client.publish(send_topic, bytes.fromhex(pkt))
                time.sleep(PACKET_INTERVAL)
        else:
            log('[MQTT] 브로커 연결 대기 중...')
            time.sleep(2)
            continue
        time.sleep(interval)


if __name__ == '__main__':
    main()
