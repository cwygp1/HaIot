"""v1.3.0 테스트 (pytest, 가상 시계). 실행: python -I -m pytest -q tests  (paho-mqtt 불필요)"""
import asyncio
import json
import os
import re
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'pycommax'))
import commax as C  # noqa: E402


class FakeClient:
    def __init__(self):
        self.pub = []

    def publish(self, topic, payload, retain=False, **k):
        self.pub.append((topic, payload if isinstance(payload, (bytes, bytearray)) else payload, retain))

    def sends(self):
        return [p.hex().upper() for t, p, _ in self.pub if t == 'ew11/send']

    def states(self):
        return {t[:-6]: p for t, p, _ in self.pub if t.endswith('/state')}


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def tick(self, s):
        self.t += s


def home_info(**over):
    tmpl = json.load(open(os.path.join(HERE, '..', 'pycommax', 'commax_devinfo.json')))
    nums = {'Light': 8, 'Thermo': 4, 'LightBreaker': 1, 'Gas': 1, 'Outlet': 2, 'Fan': 1, 'EV': 1}
    nums.update(over)
    for k, v in nums.items():
        tmpl[k]['Number'] = v
    return tmpl


@pytest.fixture
def ctl(tmp_path, monkeypatch):
    monkeypatch.setattr(C, 'DISCOVERY_LIST_FILE', str(tmp_path / 'disc.json'))
    loop = asyncio.new_event_loop()
    clock = Clock()
    fc = FakeClient()
    cfg = {'mqtt_id': 'a', 'mqtt_password': 'b', 'mqtt_server': 'x', 'elfin_server': '',
           'DEBUG': False, 'mqtt_log': False, 'bus_master': 'off'}
    c = C.Controller(cfg, home_info(), fc, loop, clock=clock)
    c.connected = True
    c.fc, c.clk = fc, clock
    yield c
    loop.close()


# ---------------------------------------------------------------- packet helpers

def test_checksum_and_split():
    assert C.checksum('31010000000000') == '3101000000000032'
    assert C.checksum('82810123230000') == '828101232300004A'
    pk, tail = C.split_packets('0201000000000003' + '828101232300004A' + 'ZZ')
    assert pk == ['0201000000000003', '828101232300004A'] and tail == 'ZZ'


def test_split_resync_and_tail():
    p1, p2 = 'B0010100000000B2', 'B1000100000000B2'
    pk, tail = C.split_packets('00' + p1 + p2)          # 선행 쓰레기 1바이트
    assert pk == [p1, p2] and tail == ''
    pk, tail = C.split_packets('FFFFFF' + p1 + p2[:10])   # 잡음 + 잘린 꼬리
    assert pk == [p1] and tail == p2[:10]
    pk, _ = C.split_packets(p2[10:] and (p2[:10] + p2[10:]))
    assert pk == [p2]


def test_split_known_heads_filter():
    bogus = C.checksum('ZZ' if False else '7F000000000000')   # 7F 헤더(미등록)
    pk, _ = C.split_packets(bogus + 'B0010100000000B2', known_heads={'B0'})
    assert pk == ['B0010100000000B2']


def test_shift_id_bytewise():
    assert C.shift_id('3101000000000032', 4, 2) == '3103000000000034'
    assert C.shift_id('B0010100000000B2', 6, 7) == 'B0010800000000B9'
    assert C.shift_id('3101000000000032', 4, 15) == '3110000000000041'      # ID 16 = 0x10
    assert C.byte_at('B0010800000000B9', 6) == 8


def test_poll_packet():
    lists = C.build_device_lists(home_info())
    assert C.poll_packet('Light', lists['Light']['list'][0]) == '3001000000000031'
    assert C.poll_packet('Outlet', lists['Outlet']['list'][1]) == C.checksum('79020200000000')
    assert C.poll_packet('EV', lists['EV']['list'][0]) is None


# ---------------------------------------------------------------- ack regex

def test_ack_regex_table():
    info = home_info()
    L = C.build_device_lists(info)
    cases = [
        ('Light', 1, 'ON', None, ['B1010200000000B4', 'B0010200000000B3'], ['B0000200000000B2']),
        ('Thermo', 0, 'ON', None, ['848101232300004C', '828301232200004B'], ['8480012322000048']),
        ('Thermo', 0, 'OFF', None, ['8480012322000048', '8284012322000048'], ['848101232300004C']),
        ('Thermo', 0, 'setTemp', 25, ['848101232500004E', '848001232500004D'], ['848101232300004C']),
        ('Fan', 0, 'speed', 1, ['F8040102000000FF', 'F6040102000000FD'], ['F6040101000000FC']),
        ('Fan', 0, 'timer', 90, ['F800010101011E1B', 'F604010101011E1E'], ['F800010101001E19', 'F8000101000000FA']),
        ('Fan', 0, 'timer_off', None, ['F8000101000000FA', 'F6040101000000FC'], ['F800010101001E19']),
        ('Gas', 0, 'OFF', None, ['9148480000000021', '9050500000000030', '9040400000000010'], ['90A0A000000000D0']),
        ('Outlet', 1, 'ON', None, ['FA0102110000000D', 'F91102110012340A'], ['F90002110000000C']),
    ]
    for name, i, action, val, yes, no in cases:
        r = C.ack_regex(name, info[name], L[name]['list'][i], action, val)
        for y in yes:
            assert re.match(r, y), (name, action, r, y)
        for n in no:
            assert not re.match(r, n), (name, action, r, n)


# ---------------------------------------------------------------- state parsing

def test_parse_light_and_ack_as_state(ctl):
    ctl.on_packet('B0010300000000B4')
    ctl.on_packet('B1000300000000B4')        # 명령응답도 상태로 반영
    st = ctl.fc.states()
    assert st['commax/Light3/power'] == b'OFF'
    ctl.on_packet('A0010100001700B9')        # byte5 0x17 변형도 매칭
    assert ctl.fc.states()['commax/LightBreaker1/power'] == b'ON'


def test_parse_thermo_action(ctl):
    ctl.on_packet('828301232200004B')
    st = ctl.fc.states()
    assert st['commax/Thermo1/power'] == b'ON' and st['commax/Thermo1/action'] == b'heating'
    assert st['commax/Thermo1/curTemp'] == b'23' and st['commax/Thermo1/setTemp'] == b'22'
    ctl.on_packet('8284022322000049')        # zooil 변형 0x84 = OFF
    st = ctl.fc.states()
    assert st['commax/Thermo2/power'] == b'OFF' and st['commax/Thermo2/action'] == b'off'
    ctl.on_packet('848103232200004C')        # ack 84 도 상태
    assert ctl.fc.states()['commax/Thermo3/action'] == b'idle'


def test_parse_gas_variants(ctl):
    for pkt, exp in [('9050500000000030', b'OFF'), ('90A0A000000000D0', b'ON'), ('9080800000000090', b'ON'),
                     ('9040400000000010', b'OFF'), ('9048480000000020', b'OFF')]:
        ctl.on_packet(pkt)
        assert ctl.fc.states()['commax/Gas1/power'] == exp, pkt


def test_parse_fan_and_timer(ctl):
    ctl.on_packet('F6040103000000FE')
    st = ctl.fc.states()
    assert st['commax/Fan1/power'] == b'ON' and st['commax/Fan1/speed'] == b'high' and st['commax/Fan1/timer'] == b'off'
    ctl.on_packet('F604010101011E1E')
    st = ctl.fc.states()
    assert st['commax/Fan1/timer_remaining'] == b'90' and st['commax/Fan1/timer'] == b'90' and st['commax/Fan1/speed'] == b'low'
    ctl.on_packet('F604010101011D1D')
    assert ctl.fc.states()['commax/Fan1/timer_remaining'] == b'89'
    ctl.on_packet('F6020101000000FA')
    assert ctl.fc.states()['commax/Fan1/preset'] == b'auto'
    ctl.on_packet('F6000100000000F7')
    st = ctl.fc.states()
    assert st['commax/Fan1/power'] == b'OFF' and st['commax/Fan1/speed'] == b'off' and st['commax/Fan1/timer'] == b'off'


def test_parse_outlet_and_ev(ctl):
    ctl.on_packet('F9010111001234' + '%02X' % (sum(bytes.fromhex('F9010111001234')) & 0xFF))
    st = ctl.fc.states()
    assert st['commax/Outlet1/power'] == b'ON' and st['commax/Outlet1/watt'] == b'123.4'
    ctl.on_packet('2301030000000027')
    st = ctl.fc.states()
    assert st['commax/EV1/floor'] == b'3' and st['commax/EV1/power'] == b'ON'   # BCD 03 -> 3층 (2026-10-15 실측)
    ctl.on_packet(C.checksum('26010146001202'))
    assert ctl.fc.states()['commax/EV1/floor'] == b'12'   # 26형: byte5 BCD


def test_unknown_header_no_exception(ctl):
    ctl.on_packet('4001000000000041')
    ctl.on_packet('8F0F0305400400EA')
    assert '8F' in ctl.unknown_seen and ctl.diag['parse_fail'] == 0


def test_on_raw_carry_over(ctl):
    p1, p2 = 'B0010100000000B2', 'B0000200000000B2'
    ctl.on_raw(bytes.fromhex('00' + p1 + p2[:10]))
    ctl.clk.tick(0.02)
    ctl.on_raw(bytes.fromhex(p2[10:]))
    st = ctl.fc.states()
    assert st['commax/Light1/power'] == b'ON' and st['commax/Light2/power'] == b'OFF'


# ---------------------------------------------------------------- commands

def test_command_without_prior_state_and_fan_timer(ctl):
    ctl.on_ha_command(['commax', 'Light5', 'power', 'command'], 'ON')
    assert ctl.queue[0]['send'] == '3105010000000037'
    ctl.on_ha_command(['commax', 'Fan1', 'timer', 'command'], '90')
    assert ctl.queue[-1]['send'] == '780103011E00009B'
    ctl.on_ha_command(['commax', 'Fan1', 'timer', 'command'], 'off')
    assert ctl.queue[-1]['send'] == '780103000000007C'
    ctl.on_ha_command(['commax', 'Thermo2', 'setTemp', 'command'], '25.0')
    assert ctl.queue[-1]['send'] == '040203250000002E'
    ctl.on_ha_command(['commax', 'Fan1', 'speed', 'command'], 'medium')   # 꺼진 상태: ON + 풍량 두 개
    sends = [q['send'] for q in ctl.queue]
    assert '780101040000007E' in sends and '780102020000007D' in sends
    ctl.on_ha_command(['commax', 'Fan1', 'speed', 'command'], 'off')
    assert ctl.queue[-1]['send'] == '780101000000007A'


# ---------------------------------------------------------------- sender timing (virtual clock)

def step(ctl, dt=0.0):
    ctl.clk.tick(dt)
    return ctl._sender_step(ctl.clk())


def test_sender_window_and_one_in_flight(ctl):
    ctl.on_ha_command(['commax', 'Light1', 'power', 'command'], 'OFF')
    ctl.on_packet('B0010200000000B3')            # 응답(헤더>=0x80) 수신 시각 기준
    assert step(ctl, 0.03) is None                # gap(60ms) 전
    assert step(ctl, 0.04) == '3101000000000032'  # 70ms: 창 안
    assert step(ctl, 0.01) is None                # in-flight
    ctl.on_ha_command(['commax', 'Light2', 'power', 'command'], 'OFF')
    assert step(ctl, 0.05) is None                # ack_wait(120ms) 전
    ctl.on_packet('B1000100000000B2')            # ack → in-flight 해제, last_rx 갱신
    assert ctl.queue and ctl.queue[0]['send'] == '3102000000000033'
    assert step(ctl, 0.07) == '3102000000000033'  # 다음 명령 창 안에서 즉시


def test_sender_misses_window_waits_for_next_rx(ctl):
    ctl.on_ha_command(['commax', 'Light1', 'power', 'command'], 'OFF')
    ctl.on_packet('B0010200000000B3')
    assert step(ctl, 0.20) is None                # 창(60~130ms) 지나침, 버스 활동 1초 미만 → 보류
    ctl.on_packet('B0010300000000B4')            # 새 수신
    assert step(ctl, 0.07) == '3101000000000032'
    # 버스가 1초 이상 조용하면 그냥 보냄
    ctl.on_ha_command(['commax', 'Light3', 'power', 'command'], 'OFF')
    ctl.on_packet('B1000100000000B2')
    assert step(ctl, 1.5) == '3103000000000034'


def test_sender_poll_wait(ctl):
    ctl.on_ha_command(['commax', 'Light1', 'power', 'command'], 'OFF')
    ctl.on_packet('3001000000000031')            # 조회 뒤에는 poll_wait(60) 이상
    assert step(ctl, 0.03) is None
    assert step(ctl, 0.04) == '3101000000000032'


def test_sender_retry_then_drop(ctl):
    ctl.cfg['max_retry'] = 3
    ctl.on_ha_command(['commax', 'Light1', 'power', 'command'], 'OFF')
    sent = 0
    for _ in range(20):
        ctl.on_packet('B0010200000000B3')         # 응답은 오지만 ack 아님
        if step(ctl, 0.07):                       # 창 안(60~130ms)
            sent += 1
        ctl.clk.tick(0.10)                        # 재전송 간격 150ms, ack_wait 120ms 경과
    assert sent == 3 and ctl.queue == [] and ctl.diag['dropped'] == 1


def test_sender_ttl_and_rate_limit_wait(ctl):
    ctl.cfg['max_tx_per_min'] = 1
    ctl.on_ha_command(['commax', 'Light1', 'power', 'command'], 'OFF')
    ctl.on_ha_command(['commax', 'Light2', 'power', 'command'], 'OFF')
    ctl.on_packet('B0010300000000B4')
    assert step(ctl, 0.07) == '3101000000000032'
    ctl.on_packet('B1000100000000B2')
    assert step(ctl, 0.07) is None                # 상한: 대기 (폐기 아님)
    assert len(ctl.queue) == 1
    step(ctl, 11.0)                               # TTL 10s 경과 → 만료 폐기
    assert ctl.queue == [] and ctl.diag['expired'] == 1


def test_tx_enable_false(ctl):
    ctl.cfg['tx_enable'] = False
    ctl.on_ha_command(['commax', 'Light1', 'power', 'command'], 'ON')
    assert step(ctl, 0.2) is None and ctl.queue == []


def test_bus_master_auto(ctl):
    ctl.cfg['bus_master'] = 'auto'
    ctl.cfg['master_after_sec'] = 10
    ctl.on_packet('3001000000000031')            # 외부 조회 1회
    assert step(ctl, 1.0) is None and ctl.master_active is False
    pkt = step(ctl, 11.0)                         # 11초 무조회 → 마스터 시작, 첫 조회 송신
    assert ctl.master_active and pkt in ctl.master_packets
    assert step(ctl, 0.05) is None                # 150ms 간격
    nxt = step(ctl, 0.15)
    assert nxt in ctl.master_packets and nxt != pkt
    ctl.on_packet('2001000000000021')            # 월패드 조회 재개(방금 보낸 조회와 다른 패킷)
    step(ctl, 0.01)
    assert ctl.master_active is False
    states = {t: p for t, p, _ in ctl.fc.pub if t == 'commax/wallpad/state'}
    assert states['commax/wallpad/state'] == b'ON'


def test_watchdog_step(ctl):
    ctl.cfg['elfin_server'] = '192.168.1.3'
    ctl.on_packet('B0010100000000B2')
    assert ctl._watchdog_step(ctl.clk() + 30) is None
    assert ctl._watchdog_step(ctl.clk() + 61) == 'probe'
    assert ctl.bus_online is False
    ctl.last_ew11_restart = ctl.clk() + 61
    assert ctl._watchdog_step(ctl.clk() + 70) is None   # backoff


# ---------------------------------------------------------------- discovery

def test_discovery_golden_ids(ctl):
    msgs = ctl.discovery_messages()
    uids = sorted(p['unique_id'] for _, p in msgs)
    expected = sorted(['commax_light%d' % i for i in range(1, 9)] + ['commax_thermo%d' % i for i in range(1, 5)] +
                      ['commax_lightbreaker1', 'commax_gas1', 'commax_gas1_state', 'commax_outlet1', 'commax_outlet2',
                       'commax_outlet1_watt', 'commax_outlet2_watt', 'commax_fan1', 'commax_fan1_timer',
                       'commax_fan1_timer_remaining', 'commax_ev1', 'commax_ev1_floor',
                       'commax_diag_success_rate', 'commax_diag_ack_latency', 'commax_diag_poll_period',
                       'commax_diag_drops', 'commax_diag_queue', 'commax_wallpad_alive'])
    assert uids == expected
    for topic, p in msgs:
        assert p['default_entity_id'].split('.')[1] == p['unique_id']
        assert p['avty_mode'] == 'all' and len(p['avty']) == 2
        assert 'device_class' not in p or p['device_class'] in ('outlet', 'power', 'opening', 'running')
    fan = [p for t, p in msgs if t.endswith('/commax_fan1/config')][0]
    assert fan['spd_rng_max'] == 3 and fan['pr_modes'] == ['auto', 'night']
    cl = [p for t, p in msgs if t.endswith('/commax_thermo1/config')][0]
    assert cl['act_t'] == 'commax/Thermo1/action/state'


def test_discovery_retire_and_republish(ctl, tmp_path):
    ctl.publish_discovery()
    first = {t for t, _, _ in ctl.fc.pub if t.startswith('homeassistant/')}
    assert all(r for t, _, r in ctl.fc.pub if t.startswith('homeassistant/'))
    # 장치 수 감소 → 옛 토픽에 빈 retain
    ctl.lists['Light']['list'] = ctl.lists['Light']['list'][:6]
    ctl.fc.pub.clear()
    ctl.publish_discovery()
    emptied = [t for t, p, r in ctl.fc.pub if p == b'' and r]
    assert 'homeassistant/light/commax_light7/config' in emptied and 'homeassistant/light/commax_light8/config' in emptied
    assert len(first) > 0


def test_consumer_sentinels_and_commands(ctl):
    async def run():
        ctl.inbox.put_nowait(('__connected__', b''))
        ctl.inbox.put_nowait(('commax/Light1/power/command', b'ON'))
        ctl.inbox.put_nowait(('ew11/recv', bytes.fromhex('B0000100000000B1')))   # OFF 상태 → ON 명령은 큐에 남음
        ctl.inbox.put_nowait(('commax/Thermo1/setTemp/command', b'abc'))      # 예외 → 루프 생존
        ctl.inbox.put_nowait(('ew11/recv', bytes.fromhex('B0000200000000B2')))
        task = ctl.loop.create_task(ctl.consumer())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    ctl.loop.run_until_complete(run())
    st = ctl.fc.states()
    assert ctl.connected and st['commax/Light2/power'] == b'OFF'
    assert any(t.startswith('homeassistant/') for t, _, _ in ctl.fc.pub)
    assert ctl.queue and ctl.queue[0]['send'] == '3101010000000033'


def test_config_defaults_match():
    cfg = json.load(open(os.path.join(HERE, '..', 'config.json')))
    for k, v in C.DEFAULTS.items():
        if k in cfg['options']:
            assert cfg['options'][k] == v, k
    assert cfg['version'] == C.VERSION


def test_diag_payload(ctl):
    d = ctl.diag_payload()
    assert d['version'] == C.VERSION and d['queue_len'] == 0 and d['success_rate'] is None
