#!/usr/bin/env python3
"""EW11 RS485 원시 패킷 캡처 (엘리베이터 호출/층 패킷 실측용)

ew11/recv(버스 수신)와 ew11/send(애드온 송신)를 구독해서
  - 모든 원시 메시지를 시각과 함께 로그 파일에 저장하고
  - 8바이트 + 합계 체크섬으로 잘라 엘리베이터 관련 패킷을 화면에 강조한다.

사용:
  pip install paho-mqtt
  MQTT_PASSWORD=... python3 ev_capture.py --host 192.168.1.28 --user cwygp
  # 실행 중 Enter 를 치면 '마커'가 로그에 찍힌다 (예: 현관 버튼 누르기 직전)
"""
import argparse
import datetime
import os
import sys
import threading

import paho.mqtt.client as mqtt

WATCH = {
    '23': 'EV 층 알림',
    '26': 'EV 층 알림(26형)',
}


def ev_label(p):
    h = p.hex().upper()
    if h[:2] in WATCH:
        return WATCH[h[:2]]
    if h.startswith('A0') and p[4] != 0x00:
        return 'EV 호출 후보 (A0, byte4={:02X})'.format(p[4])
    if h.startswith('22') and p[2] == 0x40:
        return 'EV 호출 후보 (22 xx 40)'
    if h[:2] not in ('30', 'B0', '02', '82', '76', 'F6', '77', 'F7', '20', 'A0', '10', '0F', '8F', '11', '90'):
        return '미등록 헤더'
    return None


def split(buf):
    """체크섬이 맞는 8바이트 패킷을 찾아 잘라낸다. (패킷 목록, 남은 바이트)"""
    out, i = [], 0
    while len(buf) - i >= 8:
        p = buf[i:i + 8]
        if sum(p[:7]) & 0xFF == p[7]:
            out.append(p)
            i += 8
        else:
            i += 1
    return out, buf[i:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--host', default='192.168.1.28')
    ap.add_argument('--port', type=int, default=1883)
    ap.add_argument('--user', default='cwygp')
    ap.add_argument('--out', default='ev_capture_{:%Y%m%d_%H%M%S}.log'.format(datetime.datetime.now()))
    ap.add_argument('--all', action='store_true', help='모든 패킷을 화면에 출력')
    a = ap.parse_args()
    pw = os.environ.get('MQTT_PASSWORD')
    if not pw:
        sys.exit('MQTT_PASSWORD 환경변수를 설정하세요.')

    f = open(a.out, 'a', buffering=1)
    rest = {'ew11/recv': b'', 'ew11/send': b''}
    lock = threading.Lock()

    def now():
        return datetime.datetime.now().strftime('%H:%M:%S.%f')[:-3]

    def on_connect(c, u, flags, rc, props=None):
        print('MQTT 접속 rc={}  → {}'.format(rc, a.out))
        c.subscribe([('ew11/recv', 0), ('ew11/send', 0)])

    def on_message(c, u, msg):
        with lock:
            t = now()
            d = 'RX' if msg.topic.endswith('recv') else 'TX'
            f.write('{} {} RAW {}\n'.format(t, d, msg.payload.hex().upper()))
            pkts, rest[msg.topic] = split(rest[msg.topic] + msg.payload)
            for p in pkts:
                lab = ev_label(p)
                line = '{} {} {}'.format(t, d, ' '.join('{:02X}'.format(b) for b in p))
                f.write(line + ('  <<< ' + lab if lab else '') + '\n')
                if lab or a.all:
                    print(line + ('  <<< ' + lab if lab else ''))

    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, 'ev-capture')
    c.username_pw_set(a.user, pw)
    c.on_connect, c.on_message = on_connect, on_message
    c.connect(a.host, a.port)
    c.loop_start()
    print('캡처 중. Enter = 마커, Ctrl+C = 종료')
    n = 0
    try:
        while True:
            note = input()
            n += 1
            with lock:
                f.write('{} ===== MARK {} {}\n'.format(now(), n, note))
            print('----- MARK {} {}'.format(n, note))
    except (KeyboardInterrupt, EOFError):
        pass
    c.loop_stop()
    f.close()


if __name__ == '__main__':
    main()
