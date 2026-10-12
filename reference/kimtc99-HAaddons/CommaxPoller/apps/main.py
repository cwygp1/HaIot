#!/usr/bin/env python3
"""
Commax RS485 Poller

월패드 없이 RS485 버스를 유지하기 위한 최소 기능 데몬.
EW11(TCP Server)에 접속해 주기적으로 각 기기에 상태 조회 패킷을 전송한다.
응답 파싱·상태 저장·HA 연동은 담당하지 않는다 (다른 애드온이 버스를 수신해 처리).
"""

import json
import time
import asyncio

DATA_DIR  = '/data'
APPS_DIR  = '/apps'

RESPONSE_TIMEOUT   = 0.15   # 다음 패킷 전송 전 대기 (RS485 반이중 버스 안정화용)
SKIP_POLL_PREFIXES = {'23'}  # EV는 도착 시 자체 신호 발신, 폴링 불필요


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


async def run(server, port, interval, poll_packets):
    """EW11 TCP 접속 → 폴링 루프 (자동 재접속)

    RS485는 반이중(half-duplex) 버스이므로 패킷을 하나씩 순차 전송하고,
    다음 패킷 전송 전 RESPONSE_TIMEOUT 만큼 대기해 버스 충돌을 피한다.
    수신 데이터는 버스를 비우는 용도로만 읽고 내용은 사용하지 않는다.
    """
    while True:
        writer = None
        try:
            log(f'[ELFIN] {server}:{port} 접속 중...')
            reader, writer = await asyncio.open_connection(server, port)
            log(f'[ELFIN] 접속 완료. {len(poll_packets)}개 기기 폴링 시작 (주기 {interval}s)')

            while True:
                for name, idx, pkt in poll_packets:
                    writer.write(bytes.fromhex(pkt))
                    await writer.drain()
                    try:
                        await asyncio.wait_for(reader.read(256), timeout=RESPONSE_TIMEOUT)
                    except asyncio.TimeoutError:
                        pass
                await asyncio.sleep(interval)

        except Exception as e:
            log(f'[ELFIN] 오류: {e}. 5초 후 재접속...')
        finally:
            if writer:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass
        await asyncio.sleep(5)


if __name__ == '__main__':
    log('Commax RS485 Poller 시작')

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

    asyncio.run(run(
        server       = config['elfin_server'],
        port         = config.get('elfin_port', 8899),
        interval     = config.get('poll_interval', 5),
        poll_packets = poll_packets,
    ))
