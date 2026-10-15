#!/usr/bin/env python3
"""
[TEST] COMMAX Wallpad (EW11) v1.3.0

EW11(MQTT 모드) <-> MQTT 브로커 <-> 이 애드온 <-> Home Assistant(MQTT Discovery)

v1.3.0 = 개선방향_리뷰_워크플로우.md 의 P0/P1 전부 + P2 일부(bus_master 폴백, raw 로그 자동 종료, config 현대화).
설계 메모
  - 패킷: 8바이트 고정, 합산 체크섬. 수신 스트림은 슬라이딩 재동기화 + 이월 버퍼로 자른다.
  - 상태 파싱: 상태 헤더(B0/82/A0/90/F9/F6/23/26)와 명령응답 헤더(B1/84/A2/91/FA/F8)를 같은 코드로 처리(마스크 매칭).
  - 송신: 마지막 수신 후 [gap, gap+window] 창 안에서만, 한 번에 하나(one-in-flight, ack_wait), 재전송 retry_interval, TTL.
  - 워치독: 수신 시각(last_rx)만 기준. 재시작 전 조회 프로브로 "EW11 생존/월패드 조회 중단"을 구분. 2단 availability.
  - bus_master: 외부 조회가 master_after_sec 동안 없으면 애드온이 조회를 돌려 버스를 유지(auto/on/off).
  - paho 콜백은 예외 보호 후 asyncio 큐로 전달. 단일 이벤트 루프. 가상 시계 주입 가능(clock).
"""

import asyncio
import collections
import json
import os
import re
import signal
import socket
import sys
import time

VERSION = '1.3.1'

SHARE_DIR = '/share'
CONFIG_DIR = '/data'
DATA_DIR = '/pycommax'

HA_TOPIC = 'commax'
STATE_TOPIC = HA_TOPIC + '/{}/{}/state'
AVAIL_TOPIC = HA_TOPIC + '/status'
BUS_TOPIC = HA_TOPIC + '/bus'
WALLPAD_TOPIC = HA_TOPIC + '/wallpad/state'
DIAG_TOPIC = HA_TOPIC + '/diag/state'
ELFIN_TOPIC = 'ew11'
ELFIN_RECV_TOPIC = ELFIN_TOPIC + '/recv'
ELFIN_SEND_TOPIC = ELFIN_TOPIC + '/send'
HA_STATUS_TOPIC = 'homeassistant/status'

DEVICE_FILE = SHARE_DIR + '/commax_found_device.json'
DEVICE_FILE_ALT = SHARE_DIR + '/commax_found_device_new.json'   # v1.2.x 호환
DISCOVERY_LIST_FILE = SHARE_DIR + '/commax_discovery_published.json'
TEMPLATE_FILE = DATA_DIR + '/commax_devinfo.json'

DEFAULTS = {
    'DEBUG': False, 'mqtt_log': True, 'elfin_log': False, 'raw_log_minutes': 10,
    'mqtt_port': 1883,
    'send_gap_ms': 60, 'send_window_ms': 70, 'poll_wait_ms': 60,
    'ack_wait_ms': 120, 'retry_interval_ms': 150, 'max_retry': 5, 'cmd_ttl_sec': 10,
    'tx_enable': True, 'max_tx_per_min': 120,
    'scan_seconds': 20,
    'ew11_restart_after_sec': 60, 'ew11_restart_min_interval_sec': 300,
    'bus_master': 'auto', 'master_after_sec': 10, 'master_interval_ms': 150,
    'diag_interval_sec': 60, 'purge_discovery': False,
}

FAN_SPEEDS = ['low', 'medium', 'high']
GAS_KNOWN = {'50', '48', '40', '80', 'A0', '88', '58'}
EXTRA_HEADS = {'30', '02', '20', '10', '79', '76', '77', 'F7', '40', 'C0', '0F', '8F'}


# ---------------------------------------------------------------- logging

def log(msg, level='INFO'):
    print('[{}] [{}] {}'.format(time.strftime('%Y-%m-%d %H:%M:%S'), level, msg), flush=True)


def warn(msg):
    log(msg, 'WARNING')


# ---------------------------------------------------------------- packet helpers

def checksum(hex14):
    """앞 7바이트(hex 14자리)에 합산 체크섬을 붙여 16자리 hex 반환"""
    try:
        hex14 = hex14[:14].upper()
        b = bytes.fromhex(hex14)
        if len(b) != 7:
            return None
        return hex14 + format(sum(b) & 0xFF, '02X')
    except Exception:
        return None


def valid_packet(hex16):
    return len(hex16) == 16 and checksum(hex16) == hex16.upper()


def split_packets(raw_hex, known_heads=None):
    """스트림을 8바이트 프레임으로 자른다. 체크섬이 맞지 않으면 1바이트씩 밀어 재동기화하고,
    16자리 미만 꼬리는 이월용으로 돌려준다. known_heads가 있으면 헤더도 검사(1/256 오탐 방지)."""
    raw_hex = raw_hex.upper()
    out, pos = [], 0
    while pos + 16 <= len(raw_hex):
        frag = raw_hex[pos:pos + 16]
        if valid_packet(frag) and (not known_heads or frag[:2] in known_heads):
            out.append(frag)
            pos += 16
        else:
            pos += 2
    return out, raw_hex[pos:]


def pad(value):
    value = int(value)
    return '0' + str(value) if value < 10 else str(value)


def byte_at(hex16, pos):
    """devinfo의 자릿수(1부터, hex 자리)를 바이트 값으로 읽는다"""
    i = (int(pos) - 1) // 2
    return int(hex16[2 * i:2 * i + 2], 16)


def shift_id(hex16, pos, k):
    """1번 장치 템플릿의 pos 자리(바이트)에 k를 더해 (k+1)번 장치 패킷 생성 (바이트 단위)"""
    if not hex16:
        return None
    if not pos:
        return checksum(hex16)
    b = bytearray.fromhex(hex16[:14])
    i = (int(pos) - 1) // 2
    b[i] = (b[i] + k) & 0xFF
    return checksum(b.hex().upper())


def head_or(hexs, mask):
    return '{:02X}'.format(int(hexs[:2], 16) | mask)


# ---------------------------------------------------------------- device table

def build_device_lists(dev_info):
    """devinfo(json) -> {name: {'type':..., 'list':[{id, commandON, commandOFF, stateON, stateOFF, ...}]}}"""
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
            if name == 'Fan':
                spd = dev.get('speedNUM')
                chg = dev.get('commandCHANGE')
                item['CHANGE'] = [shift_id(chg, spd, s) for s in range(3)] if chg and spd else []
            arr.append(item)
        lists[name] = {'type': dev.get('type', 'switch'), 'list': arr}
    return lists


def ack_regex(name, dev, item, action, value=None):
    """명령 성공으로 간주할 수신 패킷 정규식. 상태 헤더와 명령응답 헤더(명령|0x80) 둘 다 허용."""
    idx = '{:02X}'.format(item['id'])
    st = item.get('stateON') or item.get('stateOFF') or ''
    cmd = item.get('commandON') or item.get('commandOFF') or ''
    heads = set()
    if st:
        heads.add(st[:2])
    if cmd:
        heads.add(head_or(cmd, 0x80))
    head = '(' + '|'.join(sorted(heads)) + ')' if heads else '..'

    if name == 'Thermo':
        if action == 'ON':
            return '^' + head + '8[1-9A-F]' + idx
        if action == 'OFF':
            return '^' + head + '8[04]' + idx
        if action == 'setTemp':
            return '^' + head + '8.' + idx + '..' + pad(value)
    if name == 'Fan':
        if action == 'ON':
            return '^' + head + '0[246]' + idx
        if action == 'OFF':
            return '^' + head + '00' + idx
        if action == 'speed':
            return '^' + head + '04' + idx + '0' + str(int(value) + 1)
        if action == 'timer':          # ack F8 00 ID spd 01 HH MM == 상태 F6 04 ID spd 01 HH MM
            hh, mm = divmod(int(value), 60)
            return '^' + head + '..' + idx + '..01' + '{:02X}{:02X}'.format(hh, mm)
        if action == 'timer_off':
            return '^' + head + '..' + idx + '..000000'
    if name == 'Gas':
        return '^(91|90[0-7].)'          # 응답 91 또는 잠김 상태(bit7=0)
    if name == 'EV':
        return '^(23|26)'
    if name == 'Outlet':
        return '^' + head + '[01]' + ('1' if action == 'ON' else '0') + idx
    val = '01' if action == 'ON' else '00'
    return '^' + head + val + idx


def poll_packet(name, item):
    """월패드가 보내는 상태 조회 패킷(bus_master 폴백용). 응답헤더&0x7F + ID (Outlet은 ID 반복)"""
    st = item.get('stateON') or item.get('stateOFF')
    if not st or name == 'EV':
        return None
    head = '{:02X}'.format(int(st[:2], 16) & 0x7F)
    idx = '{:02X}'.format(item['id'])
    body = head + idx + idx + '00000000' if head == '79' else head + idx + '0000000000'
    return checksum(body)


# ---------------------------------------------------------------- controller

class Controller:
    def __init__(self, config, dev_info, mqtt_client, loop, clock=None):
        self.cfg = {**DEFAULTS, **config}
        self.dev_info = dev_info
        self.client = mqtt_client
        self.loop = loop
        self.clock = clock or time.monotonic
        self.debug = bool(self.cfg['DEBUG'])
        self.mqtt_log = bool(self.cfg['mqtt_log'])
        self.elfin_log = bool(self.cfg['elfin_log'])
        self.raw_log_until = self.clock() + float(self.cfg['raw_log_minutes']) * 60 if self.elfin_log else 0.0

        self.lists = build_device_lists(dev_info)
        self.prefix = {}            # 수신 헤더 -> 장치군 (상태 + 명령응답)
        self.poll_heads = set()     # 월패드 조회 헤더 (bus_master 감지용)
        for name, d in self.lists.items():
            it = d['list'][0]
            st = it.get('stateON') or it.get('stateOFF')
            cmd = it.get('commandON') or it.get('commandOFF')
            if st:
                self.prefix[st[:2]] = name
                self.poll_heads.add('{:02X}'.format(int(st[:2], 16) & 0x7F))
            if cmd and name != 'EV':
                self.prefix[head_or(cmd, 0x80)] = name
        if 'EV' in self.lists:
            self.prefix['26'] = 'EV'
        self.known_heads = set(self.prefix) | self.poll_heads | EXTRA_HEADS

        self.home = {}              # (dev_id, prop) -> value
        self.queue = []             # {'name','id','send','ack','count','last','enq','sent_at'}
        self.rx_tail, self.rx_tail_at = '', 0.0
        self.last_rx = self.clock()
        self.last_tx = 0.0
        self.last_sent_hex = None
        self.last_was_poll = False
        self.inflight_until = 0.0
        self.tx_times = []
        self.unknown_seen = set()
        self.gas_warned = set()
        self.ev_off_at = 0.0
        self.connected = False
        self.bus_online = None
        self.wallpad_alive = None
        self.last_ext_poll = 0.0
        self.start_at = self.clock()
        self.master_active = False
        self.master_packets = []
        self.master_idx = 0
        self.last_master_poll = 0.0
        self.last_ew11_restart = 0.0
        self.ew11_backoff = float(self.cfg['ew11_restart_min_interval_sec'])
        self.probe_pending = None
        self.published_topics = set()
        self.diag = {'tx_cmds': 0, 'tx_sends': 0, 'ack_first': 0, 'ack_total': 0, 'dropped': 0,
                     'expired': 0, 'parse_fail': 0, 'rx_total': 0, 'rx_msgs': 0, 'rx_batched': 0}
        self.ack_lat = collections.deque(maxlen=100)
        self.poll_gaps = collections.deque(maxlen=50)
        self.last_poll_at = 0.0
        asyncio.set_event_loop(loop)
        self.inbox = asyncio.Queue()

        for name, d in self.lists.items():
            for item in d['list']:
                pp = poll_packet(name, item)
                if pp:
                    self.master_packets.append(pp)

        log('----------------------')
        log('등록된 기기 목록 DEVICE_LISTS..')
        for name, d in self.lists.items():
            log('{}: {} x{}'.format(name, d['type'], len(d['list'])))
            if self.debug:
                log(' >>>> {}'.format(d['list']))
        log('헤더 맵: {}'.format(self.prefix))
        log('----------------------')

    # ---- 시간/설정
    def now(self):
        return self.clock()

    # ---- MQTT publish
    def publish(self, topic, payload, retain=True):
        if isinstance(payload, str):
            payload = payload.encode()
        try:
            self.client.publish(topic, payload, retain=retain)
        except Exception as e:
            warn('publish 실패 {}: {}'.format(topic, e))

    def set_state(self, device_id, prop, value, force=False):
        key = (device_id, prop)
        value = str(value)
        if force or self.home.get(key) != value:
            self.home[key] = value
            topic = STATE_TOPIC.format(device_id, prop)
            self.publish(topic, value)
            if self.mqtt_log:
                log('[LOG] ->> HA : {} >> {}'.format(topic, value))

    def republish_states(self):
        for (device_id, prop), value in list(self.home.items()):
            self.publish(STATE_TOPIC.format(device_id, prop), value)
        if self.wallpad_alive is not None:
            self.publish(WALLPAD_TOPIC, 'ON' if self.wallpad_alive else 'OFF')

    def set_bus(self, online):
        if self.bus_online != online:
            self.bus_online = online
            self.publish(BUS_TOPIC, 'online' if online else 'offline')
            log('[LOG] 버스 상태 -> {}'.format('online' if online else 'offline'))

    def set_wallpad_alive(self, alive):
        if self.wallpad_alive != alive:
            self.wallpad_alive = alive
            self.publish(WALLPAD_TOPIC, 'ON' if alive else 'OFF')

    # ---- Discovery
    def _dev_block(self):
        return {'identifiers': ['commax'], 'name': '코맥스 월패드', 'manufacturer': 'COMMAX',
                'model': 'RS485 via EW11 (MQTT)', 'sw_version': VERSION}

    def discovery_messages(self):
        """[(topic, payload_dict)] 순수 생성 (테스트 골든용)"""
        dev = self._dev_block()
        origin = {'name': 'commax-mqtt-test', 'sw': VERSION, 'url': 'https://github.com/cwygp1/HaIot'}
        avail = {'avty': [{'t': AVAIL_TOPIC}, {'t': BUS_TOPIC}], 'avty_mode': 'all'}
        msgs = []

        def ent(comp, oid, payload):
            payload = {'device': dev, 'o': origin, 'object_id': oid, 'unique_id': oid,
                       'default_entity_id': comp + '.' + oid, **avail, **payload}
            msgs.append(('homeassistant/{}/{}/config'.format(comp, oid), payload))

        for name, d in self.lists.items():
            typ = d['type']
            for item in d['list']:
                i = item['id']
                oid = 'commax_{}{}'.format(name.lower(), i)
                base = '{}/{}{}'.format(HA_TOPIC, name, i)
                nm = '{}{}'.format(name, i)
                if typ == 'climate':
                    ent('climate', oid, {
                        'name': nm,
                        'mode_cmd_t': base + '/power/command', 'mode_stat_t': base + '/power/state',
                        'temp_cmd_t': base + '/setTemp/command', 'temp_stat_t': base + '/setTemp/state',
                        'curr_temp_t': base + '/curTemp/state', 'act_t': base + '/action/state',
                        'min_temp': 5, 'max_temp': 40, 'temp_step': 1, 'precision': 1.0, 'temp_unit': 'C',
                        'modes': ['off', 'heat'],
                        'mode_state_template': "{% set m = {'OFF':'off','ON':'heat'} %}{{ m[value] if value in m else 'off' }}"})
                elif typ == 'fan':
                    ent('fan', oid, {
                        'name': nm,
                        'cmd_t': base + '/power/command', 'stat_t': base + '/power/state',
                        'pct_cmd_t': base + '/speed/command', 'pct_stat_t': base + '/speed/state',
                        'spd_rng_min': 1, 'spd_rng_max': 3,
                        'pct_val_tpl': "{{ {'off':0,'low':1,'medium':2,'high':3}.get(value, 0) }}",
                        'pct_cmd_tpl': "{{ 'off' if value|int == 0 else ['low','medium','high'][value|int - 1] }}",
                        'pr_mode_cmd_t': base + '/preset/command', 'pr_mode_stat_t': base + '/preset/state',
                        'pr_modes': ['auto', 'night']})
                    ent('select', oid + '_timer', {
                        'name': nm + ' Timer', 'icon': 'mdi:timer-outline',
                        'cmd_t': base + '/timer/command', 'stat_t': base + '/timer/state',
                        'options': ['off', '30', '60', '90']})
                    ent('sensor', oid + '_timer_remaining', {
                        'name': nm + ' Timer Remaining', 'icon': 'mdi:timer-sand',
                        'unit_of_measurement': 'min', 'state_class': 'measurement',
                        'stat_t': base + '/timer_remaining/state'})
                elif typ == 'button':
                    ent('button', oid, {
                        'name': nm, 'cmd_t': base + '/power/command',
                        'payload_press': 'ON' if name == 'EV' else 'OFF',
                        'icon': 'mdi:elevator' if name == 'EV' else 'mdi:valve-closed'})
                    if name == 'Gas':
                        ent('binary_sensor', oid + '_state', {
                            'name': nm + ' Open', 'device_class': 'opening',
                            'stat_t': base + '/power/state', 'pl_on': 'ON', 'pl_off': 'OFF'})
                    if name == 'EV':
                        ent('sensor', oid + '_floor', {
                            'name': nm + ' Floor', 'icon': 'mdi:elevator', 'stat_t': base + '/floor/state'})
                else:
                    payload = {'name': nm, 'cmd_t': base + '/power/command', 'stat_t': base + '/power/state'}
                    if name == 'Outlet':
                        payload['device_class'] = 'outlet'
                    ent(typ, oid, payload)
                    if name == 'Outlet':
                        ent('sensor', oid + '_watt', {
                            'name': nm + ' Watt', 'device_class': 'power', 'state_class': 'measurement',
                            'unit_of_measurement': 'W', 'stat_t': base + '/watt/state'})
        diag = [('success_rate', '성공률(첫 시도)', '%', 'mdi:check-decagram', "{{ value_json.success_rate }}"),
                ('ack_latency', 'ack 지연', 'ms', 'mdi:timer-outline', "{{ value_json.ack_latency_p50 }}"),
                ('poll_period', '월패드 조회 주기', 'ms', 'mdi:metronome', "{{ value_json.poll_period_ms }}"),
                ('drops', '명령 폐기', None, 'mdi:alert-circle-outline', "{{ value_json.dropped }}"),
                ('queue', '대기 명령', None, 'mdi:tray-full', "{{ value_json.queue_len }}")]
        for key, nm, unit, icon, tpl in diag:
            p = {'name': 'Diag ' + nm, 'icon': icon, 'stat_t': DIAG_TOPIC, 'val_tpl': tpl,
                 'entity_category': 'diagnostic'}
            if unit:
                p['unit_of_measurement'] = unit
            ent('sensor', 'commax_diag_' + key, p)
        ent('binary_sensor', 'commax_wallpad_alive', {
            'name': '월패드 조회 중', 'device_class': 'running', 'entity_category': 'diagnostic',
            'stat_t': WALLPAD_TOPIC, 'pl_on': 'ON', 'pl_off': 'OFF'})
        return msgs

    def publish_discovery(self):
        msgs = self.discovery_messages()
        current = set()
        for topic, payload in msgs:
            self.publish(topic, json.dumps(payload, ensure_ascii=False))
            current.add(topic)
        self._retire_discovery(current)
        self.publish(AVAIL_TOPIC, 'online')
        if self.bus_online is not None:
            self.publish(BUS_TOPIC, 'online' if self.bus_online else 'offline')
        log('[LOG] MQTT discovery {}개 엔티티 발행'.format(len(msgs)))

    def _retire_discovery(self, current):
        old = set()
        try:
            if os.path.exists(DISCOVERY_LIST_FILE):
                with open(DISCOVERY_LIST_FILE, encoding='utf-8') as f:
                    old = set(json.load(f))
        except Exception as e:
            warn('discovery 목록 파일 읽기 실패: {}'.format(e))
        for topic in sorted(old - current):
            self.publish(topic, b'')
            log('[LOG] 옛 discovery 제거: {}'.format(topic))
        try:
            with open(DISCOVERY_LIST_FILE, 'w', encoding='utf-8') as f:
                json.dump(sorted(current), f, indent=1)
        except Exception as e:
            warn('discovery 목록 파일 저장 실패: {}'.format(e))
        self.published_topics = current

    def purge_discovery(self):
        """옵션 purge_discovery=true: 저장된 모든 discovery 토픽에 빈 retain 발행(엔티티 초기화)"""
        try:
            with open(DISCOVERY_LIST_FILE, encoding='utf-8') as f:
                old = json.load(f)
        except Exception:
            old = []
        for topic in old:
            self.publish(topic, b'')
        log('[LOG] purge_discovery: {}개 토픽 비움'.format(len(old)))

    # ---- HA -> 명령
    def enqueue(self, name, item, send_hex, ack_re, slot=None):
        if not send_hex:
            return
        now = self.now()
        slot = item['id'] if slot is None else slot
        self.queue = [q for q in self.queue if not (q['name'] == name and q['id'] == slot)]
        self.queue.append({'name': name, 'id': slot, 'send': send_hex,
                           'ack': re.compile(ack_re), 'count': 0, 'last': 0.0, 'enq': now, 'sent_at': 0.0})
        if self.debug:
            log('[DEBUG] Queued ::: {} ack~{}'.format(send_hex, ack_re))

    def on_ha_command(self, topics, value):
        if self.mqtt_log:
            log('[LOG] HA ->> : {} -> {}'.format('/'.join(topics), value))
        m = re.match(r'([A-Za-z]+)(\d+)$', topics[1])
        if not m or m.group(1) not in self.lists:
            warn('알 수 없는 명령 토픽: {}'.format('/'.join(topics)))
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
                self.set_state(dev_id, 'setTemp', pad(t))
        elif name == 'Fan':
            if prop == 'power':
                self.enqueue(name, item, item.get('command' + onoff), ack_regex(name, dev, item, onoff))
            elif prop == 'speed':
                if v.lower() == 'off':
                    self.enqueue(name, item, item.get('commandOFF'), ack_regex(name, dev, item, 'OFF'))
                elif v.lower() in FAN_SPEEDS and item.get('CHANGE'):
                    s = FAN_SPEEDS.index(v.lower())
                    if self.home.get((dev_id, 'power')) != 'ON':
                        self.enqueue(name, item, item.get('commandON'), ack_regex(name, dev, item, 'ON'))
                    self.enqueue(name, item, item['CHANGE'][s], ack_regex(name, dev, item, 'speed', s), slot=-item['id'])
            elif prop == 'preset':
                warn('환기 프리셋({})은 제어 패킷이 알려져 있지 않아 표시만 지원합니다'.format(v))
            elif prop == 'timer':
                # 실측(2026-10-13): 78 ID 03 HH MM 00 00 = 타이머 설정(2진수 시/분), 00 00 = 해제
                mins = 0 if v.lower() in ('off', '0', 'none', 'unlimited') else int(float(v))
                mins = max(0, min(mins, 23 * 60 + 59))
                hh, mm = divmod(mins, 60)
                head = (dev.get('commandON') or '78')[:2]
                hx = checksum('{}{:02X}03{:02X}{:02X}0000'.format(head, idx, hh, mm))
                self.enqueue(name, item, hx, ack_regex(name, dev, item, 'timer' if mins else 'timer_off', mins), slot=1000 + item['id'])
                self.set_state(dev_id, 'timer', 'off' if mins == 0 else str(mins))
        else:
            self.enqueue(name, item, item.get('command' + onoff), ack_regex(name, dev, item, onoff))

    # ---- EW11 -> 패킷
    def on_raw(self, payload_bytes):
        now = self.now()
        raw = payload_bytes.hex().upper()
        self.diag['rx_msgs'] += 1
        if self.elfin_log:
            if now < self.raw_log_until:
                log('[SIGNAL] 수신: {}'.format(raw))
            else:
                self.elfin_log = False
                log('[LOG] raw 로그 {}분 경과, 자동 종료'.format(self.cfg['raw_log_minutes']))
        if self.rx_tail and now - self.rx_tail_at < 0.1:
            raw = self.rx_tail + raw
        pkts, self.rx_tail = split_packets(raw, self.known_heads)
        self.rx_tail_at = now
        if len(pkts) >= 2 and any(int(p[:2], 16) < 0x80 for p in pkts) and any(int(p[:2], 16) >= 0x80 for p in pkts):
            self.diag['rx_batched'] += 1
        for p in pkts:
            self.on_packet(p, now)

    def on_packet(self, data, now=None):
        now = self.now() if now is None else now
        self.last_rx = now
        self.diag['rx_total'] += 1
        head = int(data[:2], 16)
        self.last_was_poll = head < 0x80
        if head < 0x80 and not (data == self.last_sent_hex and now - self.last_tx < 0.3):
            if self.last_poll_at:
                gap = now - self.last_poll_at
                if 0.02 < gap < 2.0:
                    self.poll_gaps.append(gap)
            self.last_poll_at = now
            self.last_ext_poll = now
            self.set_wallpad_alive(True)
        if self.bus_online is not True:
            self.set_bus(True)
        if self.probe_pending and data.startswith(self.probe_pending):
            self.probe_pending = 'HIT'
        for q in list(self.queue):
            if q['ack'].match(data):
                self.queue.remove(q)
                self.inflight_until = 0.0
                self.diag['ack_total'] += 1
                if q['count'] == 1:
                    self.diag['ack_first'] += 1
                if q['sent_at']:
                    self.ack_lat.append((now - q['sent_at']) * 1000.0)
                if self.debug:
                    log('[DEBUG] ack {} <- {}'.format(q['send'], data))
        name = self.prefix.get(data[:2])
        if name is None:
            if data[:2] not in self.unknown_seen:
                self.unknown_seen.add(data[:2])
                if head >= 0x80 or data[:2] in ('23', '26'):   # 23/26 = 엘리베이터 층 알림 후보
                    log('[INFO] 미등록 헤더 {} 첫 수신: {}'.format(data[:2], data))
            return
        try:
            getattr(self, '_parse_' + name.lower(), self._parse_onoff)(name, data)
        except (ValueError, IndexError) as e:
            self.diag['parse_fail'] += 1
            warn('패킷 해석 실패 {} ({}): {}'.format(data, name, e))

    # 상태 파서들 ----------------------------------------------------------
    def _in_range(self, name, idx):
        return 1 <= idx <= len(self.lists[name]['list'])

    def _parse_onoff(self, name, data):          # Light, LightBreaker, 기타 [HDR][val][ID]
        idx = int(data[4:6], 16)
        if not self._in_range(name, idx):
            if self.debug:
                log('[DEBUG] <{}> 범위 밖 ID {}: {}'.format(name, idx, data))
            return
        self.set_state('{}{}'.format(name, idx), 'power', 'ON' if data[2:4] == '01' else 'OFF')

    _parse_light = _parse_onoff
    _parse_lightbreaker = _parse_onoff

    def _parse_thermo(self, name, data):
        dev = self.dev_info[name]
        idx = byte_at(data, dev.get('stateNUM', 6))
        if not self._in_range(name, idx):
            return
        b1 = int(data[2:4], 16)
        dev_id = 'Thermo{}'.format(idx)
        on = bool(b1 & 0x01)
        self.set_state(dev_id, 'power', 'ON' if on else 'OFF')
        self.set_state(dev_id, 'action', 'off' if not on else ('heating' if b1 & 0x02 else 'idle'))
        cp, sp = int(dev.get('curTemp', 7)), int(dev.get('setTemp', 9))
        cur, tgt = data[cp - 1:cp + 1], data[sp - 1:sp + 1]
        if cur.isdigit() and tgt.isdigit():
            self.set_state(dev_id, 'curTemp', pad(int(cur)))
            self.set_state(dev_id, 'setTemp', pad(int(tgt)))

    def _parse_gas(self, name, data):
        v = data[2:4]
        if v not in GAS_KNOWN and v not in self.gas_warned:
            self.gas_warned.add(v)
            warn('가스밸브 미확인 상태값 {} ({}) — 잠김/열림은 bit7로 판정합니다'.format(v, data))
        self.set_state('Gas1', 'power', 'ON' if int(v, 16) & 0x80 else 'OFF')

    def _parse_outlet(self, name, data):
        dev = self.dev_info[name]
        idx = byte_at(data, dev.get('stateNUM', 6))
        if not self._in_range(name, idx):
            return
        dev_id = 'Outlet{}'.format(idx)
        self.set_state(dev_id, 'power', 'ON' if int(data[2:4], 16) & 0x01 else 'OFF')
        if data[6:8] == '11' and data[8:14].isdigit():
            self.set_state(dev_id, 'watt', '%.1f' % (int(data[8:14]) / 10))

    def _parse_fan(self, name, data):
        idx = int(data[4:6], 16)
        if not self._in_range(name, idx):
            return
        dev_id = 'Fan{}'.format(idx)
        mode = data[2:4]
        if mode == '00':
            self.set_state(dev_id, 'power', 'OFF')
            self.set_state(dev_id, 'speed', 'off')
            self.set_state(dev_id, 'preset', 'None')
        else:
            self.set_state(dev_id, 'power', 'ON')
            spd = int(data[6:8], 16)
            if 1 <= spd <= 3:
                self.set_state(dev_id, 'speed', FAN_SPEEDS[spd - 1])
            self.set_state(dev_id, 'preset', {'02': 'auto', '06': 'night'}.get(mode, 'None'))
        remaining = (int(data[10:12], 16) * 60 + int(data[12:14], 16)) if data[8:10] != '00' else 0
        self.set_state(dev_id, 'timer_remaining', str(remaining))
        self.set_state(dev_id, 'timer', 'off' if remaining == 0 else str(min(90, ((remaining + 29) // 30) * 30)))

    def _parse_ev(self, name, data):
        dev = self.dev_info[name]
        if data not in self.unknown_seen:   # 층 인코딩 보정표용: 처음 보는 원문마다 기록
            self.unknown_seen.add(data)
            log('[INFO] EV 층 패킷 원문: {}'.format(data))
        bf = int(dev.get('BasementFloor', 0) or 0)
        if data[:2] == '26':
            floor = int(data[10:12]) if data[10:12].isdigit() else int(data[10:12], 16)
        else:
            floor = int(data[4:6], 16)
        txt = str(floor - bf + 1) if floor >= bf else 'B' + str(bf - floor)
        self.set_state('EV1', 'power', 'ON')
        self.set_state('EV1', 'floor', txt)
        self.ev_off_at = self.now() + 3

    # ---- 송신 결정: 상태만 바꾸고 보낼 hex 또는 None 반환
    def _sender_step(self, now):
        c = self.cfg
        gap = c['send_gap_ms'] / 1000.0
        window = c['send_window_ms'] / 1000.0
        poll_wait = c['poll_wait_ms'] / 1000.0
        ack_wait = c['ack_wait_ms'] / 1000.0
        retry = c['retry_interval_ms'] / 1000.0
        ttl = float(c['cmd_ttl_sec'])
        max_retry = int(c['max_retry'])

        if self.ev_off_at and now > self.ev_off_at and self.home.get(('EV1', 'power')) == 'ON':
            self.set_state('EV1', 'power', 'OFF')
            self.ev_off_at = 0.0

        mode = str(c['bus_master']).lower()
        silent_poll = now - (self.last_ext_poll or self.start_at)
        want_master = mode == 'on' or (mode == 'auto' and silent_poll > float(c['master_after_sec']))
        if want_master != self.master_active:
            self.master_active = want_master
            if want_master:
                warn('bus_master 시작: 외부 조회가 {:.0f}초 없음 → 애드온이 조회를 돌립니다'.format(silent_poll))
                self.set_wallpad_alive(False)
            else:
                log('[LOG] bus_master 종료 (월패드 조회 재개)')

        for q in list(self.queue):
            if now - q['enq'] > ttl:
                self.queue.remove(q)
                self.diag['expired'] += 1
                warn('명령 만료({}s) 폐기: {}{} {}'.format(int(ttl), q['name'], abs(q['id']) % 1000, q['send']))
            elif q['count'] >= max_retry and now - q['last'] >= retry:
                self.queue.remove(q)
                self.diag['dropped'] += 1
                warn('응답 없음 {}회, 명령 폐기: {}{} {}'.format(max_retry, q['name'], abs(q['id']) % 1000, q['send']))

        if not self.connected:
            return None
        if self.queue and not bool(c['tx_enable']):
            q = self.queue.pop(0)
            warn('수신 전용 모드(tx_enable=false)라 명령을 보내지 않음: {}{} {}'.format(q['name'], abs(q['id']) % 1000, q['send']))
            return None

        since_rx = now - self.last_rx
        need = max(gap, poll_wait) if self.last_was_poll else gap

        if self.queue:
            self.tx_times = [t for t in self.tx_times if now - t < 60]
            q = self.queue[0]
            if q['count'] == 0 and len(self.tx_times) >= int(c['max_tx_per_min']):
                return None                                     # 상한: 폐기하지 않고 대기(TTL이 상한)
            if now < self.inflight_until:
                return None                                     # one-in-flight
            if since_rx < need:
                return None
            if since_rx > need + window and since_rx < 1.0:
                return None                                     # 창을 지나침 → 다음 수신까지 보류
            if now - q['last'] < retry:
                return None
            if q['count'] == 0:
                self.tx_times.append(now)
                self.diag['tx_cmds'] += 1
            q['count'] += 1
            q['last'] = now
            q['sent_at'] = now
            self.diag['tx_sends'] += 1
            self.inflight_until = now + ack_wait
            self.last_tx = now
            self.last_sent_hex = q['send']
            self.queue.append(self.queue.pop(0))
            if self.elfin_log or self.debug:
                log('[SIGNAL] 신호 전송({}): {}'.format(q['count'], q['send']))
            return q['send']

        if self.master_active and self.master_packets:
            interval = c['master_interval_ms'] / 1000.0
            if now - self.last_master_poll >= interval and since_rx >= gap and now >= self.inflight_until:
                pkt = self.master_packets[self.master_idx % len(self.master_packets)]
                self.master_idx += 1
                self.last_master_poll = now
                self.last_tx = now
                self.last_sent_hex = pkt
                return pkt
        return None

    async def sender(self):
        while True:
            pkt = self._sender_step(self.now())
            if pkt:
                self.publish(ELFIN_SEND_TOPIC, bytes.fromhex(pkt), retain=False)
            await asyncio.sleep(0.005)

    # ---- 워치독
    def _watchdog_step(self, now):
        """반환: None | 'probe'"""
        after = float(self.cfg['ew11_restart_after_sec'])
        silent = now - self.last_rx
        if silent < after:
            return None
        if self.bus_online is not False:
            warn('{:.0f}초간 버스 신호 없음'.format(silent))
            self.set_bus(False)
        server = str(self.cfg.get('elfin_server', '') or '')
        if not server or server.startswith('192.168.x') or not bool(self.cfg['tx_enable']):
            return None
        if now - self.last_ew11_restart < self.ew11_backoff:
            return None
        return 'probe'

    async def watchdog(self):
        while True:
            await asyncio.sleep(5)
            if self._watchdog_step(self.now()) != 'probe':
                continue
            probe = checksum('30010000000000')
            self.probe_pending = 'B0'
            self.publish(ELFIN_SEND_TOPIC, bytes.fromhex(probe), retain=False)
            self.last_tx = self.now()
            self.last_sent_hex = probe
            await asyncio.sleep(0.3)
            hit = self.probe_pending == 'HIT'
            self.probe_pending = None
            self.last_ew11_restart = self.now()
            if hit:
                warn('EW11은 정상 응답. 월패드 조회가 멈춘 상태 — EW11 재시작 생략 (bus_master 폴백 대상)')
                continue
            server = str(self.cfg.get('elfin_server'))
            alive = await self.loop.run_in_executor(None, self._tcp_alive, server)
            if not alive:
                warn('EW11({}) telnet 포트 무응답 — 전원/Wi-Fi 확인 필요. 재시작 생략'.format(server))
                self.ew11_backoff = min(self.ew11_backoff * 2, 3600)
                continue
            warn('EW11({}) telnet 재시작 시도 (다음 시도까지 {:.0f}s)'.format(server, self.ew11_backoff))
            await self.loop.run_in_executor(None, self._restart_ew11, server,
                                            str(self.cfg.get('elfin_id', '')), str(self.cfg.get('elfin_password', '')))
            self.ew11_backoff = min(self.ew11_backoff * 2, 3600)

    @staticmethod
    def _tcp_alive(server):
        try:
            with socket.create_connection((server, 23), timeout=2):
                return True
        except OSError:
            return False

    @staticmethod
    def _restart_ew11(server, user, pw):
        try:
            with socket.create_connection((server, 23), timeout=3) as s:
                s.settimeout(3)

                def wait_for(token):
                    buf = b''
                    while token not in buf:
                        chunk = s.recv(256)
                        if not chunk:
                            return False
                        buf += chunk
                    return True

                if wait_for(b'login:'):
                    s.sendall(user.encode() + b'\n')
                if wait_for(b'password:'):
                    s.sendall(pw.encode() + b'\n')
                time.sleep(0.5)
                s.sendall(b'Restart\n')
            log('[LOG] EW11 재시작 명령 전송')
        except Exception as e:
            warn('EW11 재시작 실패: {}'.format(e))

    # ---- 진단
    def diag_payload(self):
        lat = sorted(self.ack_lat)
        p50 = lat[len(lat) // 2] if lat else None
        p95 = lat[min(len(lat) - 1, int(len(lat) * 0.95))] if lat else None
        pg = sorted(self.poll_gaps)
        d = self.diag
        return {
            'success_rate': round(100.0 * d['ack_first'] / d['tx_cmds'], 1) if d['tx_cmds'] else None,
            'tx_cmds': d['tx_cmds'], 'tx_sends': d['tx_sends'],
            'ack_total': d['ack_total'], 'dropped': d['dropped'], 'expired': d['expired'],
            'ack_latency_p50': round(p50, 1) if p50 is not None else None,
            'ack_latency_p95': round(p95, 1) if p95 is not None else None,
            'poll_period_ms': round(1000.0 * pg[len(pg) // 2], 1) if pg else None,
            'batched_ratio': round(100.0 * d['rx_batched'] / d['rx_msgs'], 1) if d['rx_msgs'] else None,
            'rx_total': d['rx_total'], 'parse_fail': d['parse_fail'],
            'rx_age_s': round(self.now() - self.last_rx, 1), 'queue_len': len(self.queue),
            'master_active': self.master_active, 'version': VERSION,
        }

    async def diag_loop(self):
        interval = float(self.cfg['diag_interval_sec'])
        warned_batch = False
        while True:
            await asyncio.sleep(interval)
            d = self.diag_payload()
            self.publish(DIAG_TOPIC, json.dumps(d))
            if not warned_batch and d['batched_ratio'] is not None and d['batched_ratio'] > 50 and self.diag['rx_msgs'] > 50:
                warned_batch = True
                log('[INFO] 조회+응답이 한 메시지로 묶여 옵니다({}%). EW11 Serial 설정의 Gap Time을 10ms로 낮추면 제어 성공률이 오릅니다.'.format(d['batched_ratio']))

    # ---- 수신 처리 루프 (paho 스레드 -> asyncio)
    async def consumer(self):
        while True:
            topic, payload = await self.inbox.get()
            try:
                if topic == '__connected__':
                    self.connected = True
                    log('MQTT 접속 완료')
                    if bool(self.cfg.get('purge_discovery')):
                        self.purge_discovery()
                    self.publish_discovery()
                    self.republish_states()
                elif topic == '__disconnected__':
                    self.connected = False
                    warn('MQTT 연결 끊김 (rc={}). 재접속 대기'.format(payload.decode(errors='ignore')))
                elif topic == HA_STATUS_TOPIC:
                    if payload.decode(errors='ignore').strip() == 'online':
                        log('[LOG] HA 재시작 감지 -> discovery/상태 재발행')
                        self.publish_discovery()
                        self.republish_states()
                else:
                    parts = topic.split('/')
                    if parts[0] == HA_TOPIC and parts[-1] == 'command' and len(parts) == 4:
                        self.on_ha_command(parts, payload.decode(errors='ignore'))
                    elif topic == ELFIN_RECV_TOPIC:
                        self.on_raw(payload)
            except Exception as e:
                warn('처리 중 예외 {} : {!r}'.format(topic, e))

    def mqtt_enqueue(self, topic, payload):
        self.loop.call_soon_threadsafe(self.inbox.put_nowait, (topic, payload))


# ---------------------------------------------------------------- device search

def find_device(config, template, scan_seconds, mqtt_module):
    """EW11 트래픽을 scan_seconds 동안 들어 장치 개수를 세고 파일로 저장. 하나도 못 찾으면 None"""
    dev_info = json.loads(json.dumps(template))
    state_prefix = {v['stateON'][:2]: k for k, v in dev_info.items() if v.get('stateON')}
    seen = {k: collections.Counter() for k in state_prefix.values()}
    packets = {k: set() for k in state_prefix.values()}

    def on_connect(client, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            log('MQTT 접속. {}초 동안 기기를 검색합니다..'.format(scan_seconds))
            client.subscribe(ELFIN_RECV_TOPIC, 0)
        else:
            warn('MQTT 접속 실패 rc={}'.format(reason_code))

    def on_message(client, userdata, msg):
        try:
            pkts, _ = split_packets(msg.payload.hex())
            for data in pkts:
                name = state_prefix.get(data[:2])
                if not name:
                    continue
                packets[name].add(data)
                num_pos = dev_info[name].get('stateNUM')
                seen[name][byte_at(data, num_pos) if num_pos else 1] += 1
        except Exception as e:
            warn('검색 중 예외: {!r}'.format(e))

    c = mqtt_module.Client(mqtt_module.CallbackAPIVersion.VERSION2, 'commax-mqtt-scan-' + os.environ.get('HOSTNAME', 'x')[:8])
    c.username_pw_set(config['mqtt_id'], config['mqtt_password'])
    c.on_connect, c.on_message = on_connect, on_message
    c.connect_async(config['mqtt_server'], int(config.get('mqtt_port', 1883)))
    c.loop_start()
    time.sleep(scan_seconds)
    c.loop_stop()
    c.disconnect()

    total = 0
    log('======================================')
    for name in seen:
        ids = sorted(i for i, n in seen[name].items() if n >= 2 and i > 0)   # 2회 이상 관측된 ID만
        count = max(ids) if ids else 0
        dev_info[name]['Number'] = count
        total += count
        log('DEVICE {}: {}개 (관측 ID {})  {}'.format(name, count, ids, sorted(packets[name])[:6]))
    log('======================================')
    if total == 0:
        warn('장치를 하나도 찾지 못했습니다. (EW11 전원/MQTT 설정 확인) 파일을 저장하지 않습니다.')
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
            warn('{} 는 장치 수가 모두 0이라 무시합니다.'.format(path))
    return None


# ---------------------------------------------------------------- main

async def _supervise(name, coro_factory):
    """태스크가 예외로 죽으면 로그 후 재시작"""
    while True:
        try:
            await coro_factory()
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            warn('태스크 {} 예외: {!r} — 2초 후 재시작'.format(name, e))
            await asyncio.sleep(2)


async def _health(client):
    while True:
        await asyncio.sleep(10)
        th = getattr(client, '_thread', None)
        if th is not None and not th.is_alive():
            warn('MQTT 네트워크 스레드가 죽었습니다. 프로세스를 종료해 재시작을 유도합니다.')
            os._exit(1)


def main():
    import paho.mqtt.client as mqtt

    log('[TEST] COMMAX Wallpad (EW11) v{} 시작'.format(VERSION))
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

    cid = 'commax-mqtt-' + os.environ.get('HOSTNAME', 'x')[:12]
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, cid)
    client.username_pw_set(config['mqtt_id'], config['mqtt_password'])
    client.will_set(AVAIL_TOPIC, b'offline', retain=True)
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    ctl = Controller(config, dev_info, client, loop)

    def on_connect(c, userdata, flags, reason_code, properties=None):
        try:
            if reason_code == 0:
                c.subscribe([(HA_TOPIC + '/+/+/command', 0), (ELFIN_RECV_TOPIC, 0), (HA_STATUS_TOPIC, 0)])
                ctl.mqtt_enqueue('__connected__', b'')
            else:
                warn('MQTT 접속 실패 rc={}'.format(reason_code))
        except Exception as e:
            warn('on_connect 예외: {!r}'.format(e))

    def on_disconnect(c, userdata, flags, reason_code, properties=None):
        try:
            ctl.mqtt_enqueue('__disconnected__', str(reason_code).encode())
        except Exception:
            pass

    def on_message(c, userdata, msg):
        try:
            ctl.mqtt_enqueue(msg.topic, msg.payload)
        except Exception as e:
            warn('on_message 예외: {!r}'.format(e))

    client.on_connect, client.on_disconnect, client.on_message = on_connect, on_disconnect, on_message
    client.connect_async(config['mqtt_server'], int(config.get('mqtt_port', 1883)))
    client.loop_start()

    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    tasks = [loop.create_task(_supervise('consumer', ctl.consumer)),
             loop.create_task(_supervise('sender', ctl.sender)),
             loop.create_task(_supervise('watchdog', ctl.watchdog)),
             loop.create_task(_supervise('diag', ctl.diag_loop)),
             loop.create_task(_health(client))]
    try:
        loop.run_until_complete(stop.wait())
    finally:
        log('종료 중..')
        for t in tasks:
            t.cancel()
        try:
            loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
        except Exception:
            pass
        try:
            info = client.publish(AVAIL_TOPIC, b'offline', retain=True)
            info.wait_for_publish(2)
        except Exception:
            pass
        client.disconnect()
        client.loop_stop()
        loop.close()


if __name__ == '__main__':
    main()
