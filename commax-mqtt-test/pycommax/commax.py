#!/usr/bin/env python3
"""
[TEST] COMMAX Wallpad (EW11) v1.2.4

EW11(MQTT 모드) <-> MQTT 브로커 <-> 이 애드온 <-> Home Assistant(MQTT Discovery)

v1.2.4 수정 요지 (제어불량_원인분석.md 참조)
  1. 디스커버리/상태 publish retain + HA 재시작(homeassistant/status online) 시 재발행 + LWT availability
  2. 장치 파일명 통일 (commax_found_device.json, _new.json도 읽음), 검색 결과 0개면 저장하지 않고 재검색
  3. 송신 게이트 100ms -> send_gap_ms(기본 15ms), 재전송은 retry_interval_ms 간격, max_retry 회
  4. 상태 수신 전에도 명령 송신
  5. ack 매칭을 "응답 헤더(명령+0x80)/상태 헤더 + 값 + ID" 정규식으로 (온도 바이트 완전일치 요구 제거)
  6. EW11 재시작 워치독: ew11_restart_after_sec(60) 뒤, ew11_restart_min_interval_sec(300)마다 1회, telnet timeout 3s, 별도 스레드
  7. 'fan' 오타 수정, 미등록 헤더 WARNING 로그(헤더별 1회), KeyError 제거
  8. paho 콜백 -> asyncio 큐 (단일 이벤트 루프, 락 없는 공유상태 제거)
"""

import asyncio
import json
import os
import re
import time
import telnetlib
import threading

SHARE_DIR = '/share'
CONFIG_DIR = '/data'
DATA_DIR = '/pycommax'

HA_TOPIC = 'commax'
STATE_TOPIC = HA_TOPIC + '/{}/{}/state'
AVAIL_TOPIC = HA_TOPIC + '/status'
ELFIN_TOPIC = 'ew11'
ELFIN_RECV_TOPIC = ELFIN_TOPIC + '/recv'
ELFIN_SEND_TOPIC = ELFIN_TOPIC + '/send'
HA_STATUS_TOPIC = 'homeassistant/status'

DEVICE_FILE = SHARE_DIR + '/commax_found_device.json'
DEVICE_FILE_ALT = SHARE_DIR + '/commax_found_device_new.json'   # v1.2.3 호환
TEMPLATE_FILE = DATA_DIR + '/commax_devinfo.json'

DEFAULTS = {
    'DEBUG': False, 'mqtt_log': True, 'elfin_log': False,
    'send_gap_ms': 15, 'retry_interval_ms': 300, 'max_retry': 5,
    'scan_seconds': 20,
    'ew11_restart_after_sec': 60, 'ew11_restart_min_interval_sec': 300,
}


def log(msg):
    print('[{}] {}'.format(time.strftime('%Y-%m-%d %p %I:%M:%S'), msg), flush=True)


# ---------------------------------------------------------------- packet helpers

def checksum(hex14):
    """앞 7바이트(hex 14자리)를 받아 합산 체크섬을 붙인 16자리 hex 반환"""
    try:
        hex14 = hex14[:14]
        b = bytes.fromhex(hex14)
        return hex14.upper() + format(sum(b) & 0xFF, '02X')
    except Exception:
        return None


def valid_packet(hex16):
    return len(hex16) == 16 and checksum(hex16) == hex16.upper()


def split_packets(raw_hex):
    """EW11이 붙여 보낸 스트림을 16자리씩 잘라 체크섬이 맞는 것만 반환"""
    raw_hex = raw_hex.upper()
    return [raw_hex[k:k + 16] for k in range(0, len(raw_hex), 16) if valid_packet(raw_hex[k:k + 16])]


def pad(value):
    value = int(value)
    return '0' + str(value) if value < 10 else str(value)


def shift_id(hex16, pos, k):
    """1번 장치 템플릿의 pos(1부터 세는 hex 자릿수)에 k를 더해 (k+1)번 장치 패킷 생성"""
    if not hex16 or not pos:
        return checksum(hex16) if hex16 else None
    pos = int(pos)
    digit = int(hex16[pos - 1], 16) + k
    return checksum(hex16[:pos - 1] + format(digit, 'X') + hex16[pos:])


# ---------------------------------------------------------------- device table

def build_device_lists(dev_info):
    """devinfo(json) -> {name: {'type':..., 'list':[{commandON, commandOFF, stateON, stateOFF, id}, ...]}}"""
    lists = {}
    for name, dev in dev_info.items():
        num = int(dev.get('Number', 0) or 0)
        if num <= 0:
            continue
        arr = []
        for k in range(num):
            item = {'id': k + 1}
            for cmd in ('command', 'state'):
                for onoff in ('ON', 'OFF'):
                    hx = dev.get(cmd + onoff)
                    item[cmd + onoff] = shift_id(hx, dev.get(cmd + 'NUM'), k) if hx else None
            arr.append(item)
        if name == 'Fan':
            spd = dev.get('speedNUM')
            for item in arr:
                base_on = item['stateON']
                item['stateON_speed'] = [shift_id(base_on, spd, s) for s in range(3)] if base_on and spd else []
                chg = dev.get('commandCHANGE')
                item['CHANGE'] = [shift_id(chg, spd, s) for s in range(3)] if chg and spd else []
        lists[name] = {'type': dev.get('type', 'switch'), 'list': arr}
    return lists


def ack_regex(name, dev, item, action, value=None):
    """명령 성공으로 간주할 수신 패킷 정규식 (16자리 hex 기준).
    상태 헤더(예 B0)와 명령응답 헤더(명령헤더|0x80, 예 B1) 둘 다 허용."""
    idx = '{:02X}'.format(item['id'])
    st_on = item.get('stateON') or ''
    cmd_on = item.get('commandON') or item.get('commandOFF') or ''
    heads = set()
    if st_on:
        heads.add(st_on[:2])
    if cmd_on:
        heads.add('{:02X}'.format(int(cmd_on[:2], 16) | 0x80))
    head = '(' + '|'.join(sorted(heads)) + ')' if heads else '..'

    if name == 'Thermo':
        if action == 'ON':
            return '^' + head + '8[13]' + idx
        if action == 'OFF':
            return '^' + head + '80' + idx
        if action == 'setTemp':
            return '^' + head + '8[13]' + idx + '..' + pad(value)
    if name == 'Fan':
        if action == 'ON':
            return '^' + head + '0[246]' + idx
        if action == 'OFF':
            return '^' + head + '00' + idx
        if action == 'speed':
            return '^' + head + '04' + idx + '0' + str(int(value) + 1)
    if name == 'Gas':
        return '^(91|90' + ((item.get('stateOFF') or '')[2:6] or '') + ')'
    if name == 'EV':
        return '^23'
    if name == 'Outlet':
        return '^' + head + '[01]' + ('1' if action == 'ON' else '0') + idx
    # Light, LightBreaker, 기타 ON/OFF 장치: [헤더][값][ID]
    val = '01' if action == 'ON' else '00'
    return '^' + head + val + idx


# ---------------------------------------------------------------- controller

class Controller:
    def __init__(self, config, dev_info, mqtt_client, loop):
        self.cfg = {**DEFAULTS, **config}
        self.dev_info = dev_info
        self.client = mqtt_client
        self.loop = loop
        self.debug = bool(self.cfg['DEBUG'])
        self.mqtt_log = bool(self.cfg['mqtt_log'])
        self.elfin_log = bool(self.cfg['elfin_log'])

        self.lists = build_device_lists(dev_info)
        self.prefix = {}
        for name, d in self.lists.items():
            st = d['list'][0].get('stateON') or d['list'][0].get('stateOFF')
            if st:
                self.prefix[st[:2]] = name
        self.home = {}          # HA에 알린 마지막 상태
        self.queue = []         # {'name','id','send','ack'(regex),'count','last'}
        self.last_recv = time.monotonic()
        self.last_ew11_restart = 0.0
        self.unknown_seen = set()
        self.ev_off_at = 0.0
        asyncio.set_event_loop(loop)          # Python<3.10: Queue가 생성 시점 루프에 묶임
        self.inbox = asyncio.Queue()

        log('----------------------')
        log('등록된 기기 목록 DEVICE_LISTS..')
        for name, d in self.lists.items():
            log('{}: {} x{}'.format(name, d['type'], len(d['list'])))
            if self.debug:
                log(' >>>> {}'.format(d['list']))
        log('----------------------')

    # ---- MQTT publish helpers
    def publish(self, topic, payload, retain=True):
        if isinstance(payload, str):
            payload = payload.encode()
        self.client.publish(topic, payload, retain=retain)

    def set_state(self, device_id, prop, value, force=False):
        key = device_id + prop
        if force or self.home.get(key) != value:
            self.home[key] = value
            topic = STATE_TOPIC.format(device_id, prop)
            self.publish(topic, str(value))
            if self.mqtt_log:
                log('[LOG] ->> HA : {} >> {}'.format(topic, value))

    def republish_states(self):
        for key, value in list(self.home.items()):
            # key = DeviceN + prop ; prop은 power/setTemp/curTemp/speed/watt/floor 중 하나
            for prop in ('power', 'setTemp', 'curTemp', 'speed', 'watt', 'floor'):
                if key.endswith(prop):
                    self.publish(STATE_TOPIC.format(key[:-len(prop)], prop), str(value))
                    break

    # ---- Discovery
    def publish_discovery(self):
        dev = {'identifiers': 'commax', 'name': '코맥스 월패드', 'manufacturer': 'commax'}
        avail = {'avty_t': AVAIL_TOPIC}
        for name, d in self.lists.items():
            typ = d['type']
            for item in d['list']:
                i = item['id']
                oid = 'commax_{}{}'.format(name.lower(), i)
                base = '{}/{}{}'.format(HA_TOPIC, name, i)
                if typ == 'climate':
                    payload = {
                        'device': dev, 'name': '{}{}'.format(name, i), 'object_id': oid, 'unique_id': oid,
                        'mode_cmd_t': base + '/power/command', 'mode_stat_t': base + '/power/state',
                        'temp_cmd_t': base + '/setTemp/command', 'temp_stat_t': base + '/setTemp/state',
                        'curr_temp_t': base + '/curTemp/state',
                        'min_temp': 5, 'max_temp': 40, 'temp_step': 1, 'modes': ['off', 'heat'],
                        'mode_state_template': "{% set m = {'OFF':'off','ON':'heat'} %}{{ m[value] if value in m else 'off' }}",
                        **avail}
                elif typ == 'fan':
                    payload = {
                        'device': dev, 'name': '{}{}'.format(name, i), 'object_id': oid, 'unique_id': oid,
                        'cmd_t': base + '/power/command', 'stat_t': base + '/power/state',
                        'pr_mode_cmd_t': base + '/speed/command', 'pr_mode_stat_t': base + '/speed/state',
                        'pr_modes': ['low', 'medium', 'high'], **avail}
                elif typ == 'button':
                    payload = {
                        'device': dev, 'name': '{}{}'.format(name, i), 'object_id': oid, 'unique_id': oid,
                        'cmd_t': base + '/power/command', 'payload_press': 'ON' if name == 'EV' else 'OFF', **avail}
                else:
                    payload = {
                        'device': dev, 'name': '{}{}'.format(name, i), 'object_id': oid, 'unique_id': oid,
                        'cmd_t': base + '/power/command', 'stat_t': base + '/power/state', **avail}
                    if name == 'Outlet':
                        payload['device_class'] = 'outlet'
                topic = 'homeassistant/{}/{}/config'.format(typ, oid)
                self.publish(topic, json.dumps(payload, ensure_ascii=False))
                if name == 'Outlet':
                    self.publish('homeassistant/sensor/{}_watt/config'.format(oid), json.dumps({
                        'device': dev, 'name': '{}{} Watt'.format(name, i),
                        'object_id': oid + '_watt', 'unique_id': oid + '_watt',
                        'device_class': 'power', 'state_class': 'measurement', 'unit_of_measurement': 'W',
                        'stat_t': base + '/watt/state', 'entity_category': 'diagnostic', **avail}, ensure_ascii=False))
                if name == 'EV':
                    self.publish('homeassistant/sensor/{}_floor/config'.format(oid), json.dumps({
                        'device': dev, 'name': '{}{} Floor'.format(name, i),
                        'object_id': oid + '_floor', 'unique_id': oid + '_floor',
                        'stat_t': base + '/floor/state', 'icon': 'mdi:elevator', **avail}, ensure_ascii=False))
        self.publish(AVAIL_TOPIC, 'online')
        log('[LOG] MQTT discovery {}개 장치군 발행'.format(len(self.lists)))

    # ---- HA -> 명령
    def enqueue(self, name, item, send_hex, ack_re):
        if not send_hex:
            return
        # 같은 장치에 대한 이전 명령은 교체
        self.queue = [q for q in self.queue if not (q['name'] == name and q['id'] == item['id'])]
        self.queue.append({'name': name, 'id': item['id'], 'send': send_hex,
                           'ack': re.compile(ack_re), 'count': 0, 'last': 0.0})
        if self.debug:
            log('[DEBUG] Queued ::: {} ack~{}'.format(send_hex, ack_re))

    def on_ha_command(self, topics, value):
        if self.mqtt_log:
            log('[LOG] HA ->> : {} -> {}'.format('/'.join(topics), value))
        m = re.match(r'([A-Za-z]+)(\d+)$', topics[1])
        if not m or m.group(1) not in self.lists:
            log('[WARNING] 알 수 없는 명령 토픽: {}'.format('/'.join(topics)))
            return
        name, idx = m.group(1), int(m.group(2))
        prop = topics[2]
        items = self.lists[name]['list']
        if not 1 <= idx <= len(items):
            return
        item = items[idx - 1]
        dev = self.dev_info[name]
        dev_id = '{}{}'.format(name, idx)
        v = str(value).strip()
        onoff = 'ON' if v.lower() in ('on', 'heat', '1', 'true') else 'OFF'

        if name == 'Thermo':
            if prop == 'power':
                hx = shift_id(dev.get('command' + onoff), dev.get('commandNUM'), idx - 1)
                self.enqueue(name, item, hx, ack_regex(name, dev, item, onoff))
            elif prop == 'setTemp':
                t = int(float(v))
                hx = shift_id(dev.get('commandCHANGE'), dev.get('commandNUM'), idx - 1)
                cha = int(dev.get('chaTemp', 7))
                hx = checksum(hx[:cha - 1] + pad(t) + hx[cha + 1:])
                self.enqueue(name, item, hx, ack_regex(name, dev, item, 'setTemp', t))
                self.set_state(dev_id, 'setTemp', pad(t))      # 낙관적 선반영
        elif name == 'Fan':
            if prop == 'power':
                self.enqueue(name, item, item.get('command' + onoff), ack_regex(name, dev, item, onoff))
            elif prop == 'speed':
                speeds = ['low', 'medium', 'high']
                if v.lower() in speeds and item.get('CHANGE'):
                    s = speeds.index(v.lower())
                    self.enqueue(name, item, item['CHANGE'][s], ack_regex(name, dev, item, 'speed', s))
        else:
            self.enqueue(name, item, item.get('command' + onoff), ack_regex(name, dev, item, onoff))

    # ---- EW11 -> 패킷
    def on_packet(self, data):
        self.last_recv = time.monotonic()
        for q in list(self.queue):
            if q['ack'].match(data):
                self.queue.remove(q)
                if self.debug:
                    log('[DEBUG] ack {} <- {}'.format(q['send'], data))
        name = self.prefix.get(data[:2])
        if name is None:
            head = data[:2]
            if head not in self.unknown_seen:
                self.unknown_seen.add(head)
                # 월패드 조회(하위7비트)·명령응답(헤더+1 등)은 정상 트래픽. 처음 1회만 안내.
                log('[INFO] 미등록 헤더 {} 첫 수신: {} (조회/응답 패킷이면 정상)'.format(head, data))
            return
        dev = self.dev_info[name]
        d = self.lists[name]
        try:
            if name == 'Thermo':
                idx = int(data[int(dev['stateNUM']) - 1], 16)
                onoff = 'ON' if int(data[int(dev['stateONOFFNUM']) - 1], 16) > 0 else 'OFF'
                cur, tgt = data[int(dev['curTemp']) - 1:int(dev['curTemp']) + 1], data[int(dev['setTemp']) - 1:int(dev['setTemp']) + 1]
                dev_id = 'Thermo{}'.format(idx)
                self.set_state(dev_id, 'power', onoff)
                self.set_state(dev_id, 'curTemp', pad(int(cur)))
                self.set_state(dev_id, 'setTemp', pad(int(tgt)))
            elif name == 'Fan':
                idx = int(data[5], 16)
                dev_id = 'Fan{}'.format(idx)
                if data[2:4] == '00':
                    self.set_state(dev_id, 'power', 'OFF')
                else:
                    self.set_state(dev_id, 'power', 'ON')
                    spd = int(data[7], 16)
                    if 1 <= spd <= 3:
                        self.set_state(dev_id, 'speed', ['low', 'medium', 'high'][spd - 1])
            elif name == 'Outlet':
                idx = int(data[int(dev['stateNUM']) - 1], 16)
                dev_id = 'Outlet{}'.format(idx)
                on = int(data[3], 16) & 1
                self.set_state(dev_id, 'power', 'ON' if on else 'OFF')
                if data[6:8] == '11':
                    try:
                        self.set_state(dev_id, 'watt', '%.1f' % (int(data[8:14]) / 10))
                    except ValueError:
                        pass
            elif name == 'EV':
                floor = int(data[4:6], 16)
                bf = int(dev.get('BasementFloor', 0) or 0)
                txt = str(floor - bf + 1) if floor >= bf else 'B' + str(bf - floor)
                self.set_state('EV1', 'power', 'ON')
                self.set_state('EV1', 'floor', txt)
                self.ev_off_at = time.monotonic() + 3
            elif name == 'Gas':
                for item in d['list']:
                    if item.get('stateOFF') and data[:6] == item['stateOFF'][:6]:
                        self.set_state('Gas{}'.format(item['id']), 'power', 'OFF')
                    elif item.get('stateON') and data[:6] == item['stateON'][:6]:
                        self.set_state('Gas{}'.format(item['id']), 'power', 'ON')
            else:   # Light, LightBreaker
                for item in d['list']:
                    if data == item.get('stateOFF'):
                        self.set_state('{}{}'.format(name, item['id']), 'power', 'OFF')
                        return
                    if data == item.get('stateON'):
                        self.set_state('{}{}'.format(name, item['id']), 'power', 'ON')
                        return
                if self.debug:
                    log('[DEBUG] <{}> 등록되지 않은 상태 패킷: {}'.format(name, data))
        except (ValueError, IndexError) as e:
            log('[WARNING] 패킷 해석 실패 {} ({}): {}'.format(data, name, e))

    # ---- 송신 루프
    async def sender(self):
        gap = self.cfg['send_gap_ms'] / 1000.0
        retry = self.cfg['retry_interval_ms'] / 1000.0
        max_retry = int(self.cfg['max_retry'])
        while True:
            now = time.monotonic()
            if self.ev_off_at and now > self.ev_off_at and self.home.get('EV1power') == 'ON':
                self.set_state('EV1', 'power', 'OFF')
                self.ev_off_at = 0.0
            if self.queue and now - self.last_recv >= gap:
                q = self.queue[0]
                if now - q['last'] >= retry:
                    if q['count'] >= max_retry:
                        self.queue.pop(0)
                        log('[WARNING] 응답 없음 {}회, 명령 폐기: {}{} {}'.format(max_retry, q['name'], q['id'], q['send']))
                        continue
                    self.client.publish(ELFIN_SEND_TOPIC, bytes.fromhex(q['send']))
                    q['count'] += 1
                    q['last'] = now
                    self.last_recv = now           # 내 송신 직후에도 gap 만큼 쉬기
                    if self.elfin_log:
                        log('[SIGNAL] 신호 전송({}): {}'.format(q['count'], q['send']))
                    # 회전: 여러 명령이 있으면 번갈아
                    self.queue.append(self.queue.pop(0))
            await asyncio.sleep(0.005)

    # ---- EW11 워치독
    async def watchdog(self):
        after = float(self.cfg['ew11_restart_after_sec'])
        min_iv = float(self.cfg['ew11_restart_min_interval_sec'])
        server = self.cfg.get('elfin_server', '')
        warned = False
        while True:
            await asyncio.sleep(5)
            silent = time.monotonic() - self.last_recv
            if silent < after:
                warned = False
                continue
            if not warned:
                log('[WARNING] {:.0f}초간 EW11 신호 없음'.format(silent))
                warned = True
            if not server or server.startswith('192.168.x') or time.monotonic() - self.last_ew11_restart < min_iv:
                continue
            self.last_ew11_restart = time.monotonic()
            log('[WARNING] EW11({}) telnet 재시작 시도'.format(server))
            await self.loop.run_in_executor(None, self._restart_ew11, server,
                                            self.cfg.get('elfin_id', ''), self.cfg.get('elfin_password', ''))

    @staticmethod
    def _restart_ew11(server, user, pw):
        try:
            t = telnetlib.Telnet(server, 23, timeout=3)
            t.read_until(b'login:', 3)
            t.write(user.encode() + b'\n')
            t.read_until(b'password:', 3)
            t.write(pw.encode() + b'\n')
            t.write(b'Restart\n')
            t.close()
            log('[LOG] EW11 재시작 명령 전송')
        except Exception as e:
            log('[WARNING] EW11 재시작 실패: {}'.format(e))

    # ---- 수신 처리 루프 (paho 스레드 -> asyncio)
    async def consumer(self):
        while True:
            topic, payload = await self.inbox.get()
            try:
                parts = topic.split('/')
                if topic == HA_STATUS_TOPIC:
                    if payload.decode(errors='ignore').strip() == 'online':
                        log('[LOG] HA 재시작 감지 -> discovery/상태 재발행')
                        self.publish_discovery()
                        self.republish_states()
                elif parts[0] == HA_TOPIC and parts[-1] == 'command' and len(parts) == 4:
                    self.on_ha_command(parts, payload.decode(errors='ignore'))
                elif topic == ELFIN_RECV_TOPIC:
                    raw = payload.hex().upper()
                    if self.elfin_log:
                        log('[SIGNAL] 수신: {}'.format(raw))
                    for pkt in split_packets(raw):
                        self.on_packet(pkt)
            except Exception as e:
                log('[ERROR] 처리 중 예외 {} : {}'.format(topic, e))

    def mqtt_enqueue(self, topic, payload):
        self.loop.call_soon_threadsafe(self.inbox.put_nowait, (topic, payload))


# ---------------------------------------------------------------- device search

def find_device(config, template, scan_seconds, mqtt_module):
    """EW11 트래픽을 scan_seconds 동안 들어 장치 개수를 세고 파일로 저장. 하나도 못 찾으면 None"""
    dev_info = json.loads(json.dumps(template))
    state_prefix = {v['stateON'][:2]: k for k, v in dev_info.items() if v.get('stateON')}
    found = {k: 0 for k in state_prefix.values()}
    packets = {k: set() for k in state_prefix.values()}

    def on_connect(client, userdata, flags, rc, *a):
        if rc == 0:
            log('MQTT 접속. {}초 동안 기기를 검색합니다..'.format(scan_seconds))
            client.subscribe(ELFIN_RECV_TOPIC, 0)
        else:
            log('MQTT 접속 실패 rc={}'.format(rc))

    def on_message(client, userdata, msg):
        for data in split_packets(msg.payload.hex()):
            name = state_prefix.get(data[:2])
            if not name:
                continue
            packets[name].add(data)
            num_pos = dev_info[name].get('stateNUM')
            found[name] = max(found[name], int(data[int(num_pos) - 1], 16)) if num_pos else 1

    c = mqtt_module.Client(mqtt_module.CallbackAPIVersion.VERSION1, 'commax-mqtt-scan')
    c.username_pw_set(config['mqtt_id'], config['mqtt_password'])
    c.on_connect, c.on_message = on_connect, on_message
    c.connect_async(config['mqtt_server'], int(config.get('mqtt_port', 1883)))
    c.loop_start()
    time.sleep(scan_seconds)
    c.loop_stop()
    c.disconnect()

    total = 0
    log('======================================')
    for name in found:
        dev_info[name]['Number'] = found[name]
        total += found[name]
        log('DEVICE {}: {}개  {}'.format(name, found[name], sorted(packets[name])[:6]))
    log('======================================')
    if total == 0:
        log('[WARNING] 장치를 하나도 찾지 못했습니다. (EW11 전원/MQTT 설정 확인) 파일을 저장하지 않습니다.')
        return None
    with open(DEVICE_FILE, 'w', encoding='utf-8') as f:
        json.dump(dev_info, f, indent='\t', ensure_ascii=False)
    log('기기 목록 저장: {} (개수만 반영됨. 가스/일괄소등 상태값은 필요시 직접 수정)'.format(DEVICE_FILE))
    return dev_info


def load_device_file():
    for path in (DEVICE_FILE_ALT, DEVICE_FILE):
        if os.path.exists(path):
            with open(path, encoding='utf-8') as f:
                info = json.load(f)
            if sum(int(v.get('Number', 0) or 0) for v in info.values()) > 0:
                log('기기 정보 파일 사용: {}'.format(path))
                return info
            log('[WARNING] {} 는 장치 수가 모두 0이라 무시합니다.'.format(path))
    return None


# ---------------------------------------------------------------- main

def main():
    import paho.mqtt.client as mqtt

    log("[TEST] COMMAX Wallpad (EW11) v1.2.4 시작")
    with open(CONFIG_DIR + '/options.json') as f:
        config = {**DEFAULTS, **json.load(f)}
    with open(TEMPLATE_FILE, encoding='utf-8') as f:
        template = json.load(f)

    dev_info = load_device_file()
    while dev_info is None:
        dev_info = find_device(config, template, int(config['scan_seconds']), mqtt)
        if dev_info is None:
            log('30초 후 재검색합니다.')
            time.sleep(30)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, 'commax-mqtt')
    client.username_pw_set(config['mqtt_id'], config['mqtt_password'])
    client.will_set(AVAIL_TOPIC, b'offline', retain=True)
    ctl = Controller(config, dev_info, client, loop)

    def on_connect(c, userdata, flags, rc, *a):
        if rc == 0:
            log('MQTT 접속 완료')
            c.subscribe([(HA_TOPIC + '/+/+/command', 0), (ELFIN_RECV_TOPIC, 0), (HA_STATUS_TOPIC, 0)])
            ctl.publish_discovery()
            ctl.republish_states()
        else:
            log('MQTT 접속 실패 rc={}'.format(rc))

    def on_message(c, userdata, msg):
        ctl.mqtt_enqueue(msg.topic, msg.payload)

    client.on_connect, client.on_message = on_connect, on_message
    client.connect_async(config['mqtt_server'], int(config.get('mqtt_port', 1883)))
    client.loop_start()

    try:
        loop.run_until_complete(asyncio.gather(ctl.consumer(), ctl.sender(), ctl.watchdog()))
    finally:
        client.publish(AVAIL_TOPIC, b'offline', retain=True)
        client.loop_stop()


if __name__ == '__main__':
    main()
