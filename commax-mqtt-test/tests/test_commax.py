"""오프라인 테스트: python -I tests/test_commax.py (paho-mqtt 2.x 필요)"""
import sys, json, asyncio, re, time
import os
HERE=os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE,'..','pycommax'))
import commax as C

class FakeClient:
    def __init__(self): self.pub = []
    def publish(self, topic, payload, retain=False, **k):
        self.pub.append((topic, payload if isinstance(payload,(bytes,bytearray)) else payload, retain))

fails = 0
def check(cond, msg):
    global fails
    print(('OK   ' if cond else 'FAIL ') + msg)
    if not cond: fails += 1

# 1. checksum / split
check(C.checksum('31010000000000') == '3101000000000032', 'checksum light off')
check(C.checksum('82810123230000') == '8281012323000049'[:14] + '4A', 'checksum thermo (cafe log 4A)')
check(C.split_packets('0201000000000003' + '828101232300004A' + 'ZZ') == ['0201000000000003','828101232300004A'], 'split_packets')
check(C.shift_id('3101000000000032', 4, 2) == '3103000000000034', 'shift_id light3 off')
check(C.shift_id('B0010100000000B2', 6, 7) == 'B0010800000000B9', 'shift_id light8 on')

tmpl = json.load(open(os.path.join(HERE,'..','pycommax','commax_devinfo.json')))
info = json.loads(json.dumps(tmpl))
info['Light']['Number']=8; info['Thermo']['Number']=4; info['LightBreaker']['Number']=1; info['Fan']['Number']=1; info['Gas']['Number']=1; info['Outlet']['Number']=2; info['EV']['Number']=1
lists = C.build_device_lists(info)
check(len(lists['Light']['list'])==8 and lists['Light']['list'][7]['commandON']=='310801000000003A', 'build light8 cmd')
check(lists['Fan']['list'][0]['commandON']=='780101040000007E', 'fan commandON checksum recomputed (7E)')
check(lists['Fan']['list'][0]['CHANGE']==['780102010000007C','780102020000007D','780102030000007E'], 'fan CHANGE list')
check(lists['Fan']['list'][0]['stateON_speed']==['F6040101000000FC','F6040102000000FD','F6040103000000FE'], 'fan speed states')

# 2. ack regex
li = lists['Light']['list'][1]
r = C.ack_regex('Light', info['Light'], li, 'ON')
check(re.match(r,'B1010200000000B4') and re.match(r,'B0010200000000B3') and not re.match(r,'B0000200000000B2'), 'light ack regex '+r)
th = lists['Thermo']['list'][0]
r = C.ack_regex('Thermo', info['Thermo'], th, 'ON')
check(re.match(r,'848101232300004C') and re.match(r,'828301232200004B') and not re.match(r,'8480012322000048'), 'thermo on ack '+r)
r = C.ack_regex('Thermo', info['Thermo'], th, 'setTemp', 25)
check(re.match(r,'848101232500004E') and not re.match(r,'848101232300004C'), 'thermo settemp ack '+r)
fa = lists['Fan']['list'][0]
r = C.ack_regex('Fan', info['Fan'], fa, 'speed', 1)
check(re.match(r,'F8040102000000FF') and re.match(r,'F6040102000000FD'), 'fan speed ack '+r)
r = C.ack_regex('Gas', info['Gas'], lists['Gas']['list'][0], 'OFF')
check(re.match(r,'9148480000000021') and re.match(r,'9050500000000030'), 'gas ack '+r)
r = C.ack_regex('Outlet', info['Outlet'], lists['Outlet']['list'][1], 'ON')
check(re.match(r,'FA0102110000000D') and re.match(r,'F91102110012340A'), 'outlet ack '+r)

# 3. controller flow
loop = asyncio.new_event_loop()
fc = FakeClient()
cfg = {'mqtt_id':'a','mqtt_password':'b','mqtt_server':'x','elfin_server':'192.168.x.x','DEBUG':True,'send_gap_ms':15,'poll_wait_ms':40,'retry_interval_ms':100,'max_retry':3}
ctl = C.Controller(cfg, info, fc, loop)
ctl.publish_discovery()
topics = [t for t,_,_ in fc.pub]
check(all(r for _,_,r in fc.pub), 'discovery retained')
check('homeassistant/light/commax_light1/config' in topics and 'homeassistant/climate/commax_thermo4/config' in topics and 'homeassistant/sensor/commax_outlet1_watt/config' in topics, 'discovery topics')
pl = json.loads([p for t,p,_ in fc.pub if t=='homeassistant/light/commax_light1/config'][0])
check('device_class' not in pl and pl['avty_t']=='commax/status' and pl['unique_id']=='commax_light1', 'light payload clean + availability')

# state packets
fc.pub.clear()
ctl.on_packet('B0010300000000B4'); ctl.on_packet('828301232200004B'); ctl.on_packet('F9010111001234' + '%02X'%(sum(bytes.fromhex('F9010111001234'))&0xFF)); ctl.on_packet('F6040102000000FD'); ctl.on_packet('0201000000000003'); ctl.on_packet('848101232300004C')
st = {t[:-6] if t.endswith('/state') else t:p for t,p,_ in fc.pub}
check(st.get('commax/Light3/power')==b'ON', 'light3 state')
check(st.get('commax/Thermo1/power')==b'ON' and st.get('commax/Thermo1/curTemp')==b'23' and st.get('commax/Thermo1/setTemp')==b'22', 'thermo1 state '+str({k:v for k,v in st.items() if 'Thermo' in k}))
check(st.get('commax/Outlet1/power')==b'ON' and st.get('commax/Outlet1/watt')==b'123.4', 'outlet watt '+str(st.get('commax/Outlet1/watt')))
check(st.get('commax/Fan1/power')==b'ON' and st.get('commax/Fan1/speed')==b'medium', 'fan state')
check(all(r for _,_,r in fc.pub), 'states retained')

# command without prior state (light5 never seen) -> must be queued and sent
fc.pub.clear()
ctl.on_ha_command(['commax','Light5','power','command'], 'ON')
check(len(ctl.queue)==1 and ctl.queue[0]['send']=='3105010000000037', 'command queued without prior state')
async def run_sender(t):
    task = loop.create_task(ctl.sender()); await asyncio.sleep(t); task.cancel()
    try: await task
    except asyncio.CancelledError: pass
ctl.last_recv = time.monotonic() - 1
loop.run_until_complete(run_sender(0.05))
sent = [p for t,p,_ in fc.pub if t=='ew11/send']
check(sent == [bytes.fromhex('3105010000000037')], 'sent once within 50ms (gap 15ms) '+str(sent))
# ack arrives -> queue cleared
ctl.on_packet('B1010500000000B7')
check(ctl.queue == [], 'ack clears queue')
# no ack -> retries then drop
fc.pub.clear(); ctl.on_ha_command(['commax','Thermo2','setTemp','command'], '25.0')
ctl.last_recv = time.monotonic() - 1
loop.run_until_complete(run_sender(0.45))
sent = [p.hex().upper() for t,p,_ in fc.pub if t=='ew11/send']
check(len(sent)==3 and sent[0]=='040203250000002E' and ctl.queue==[], 'thermo settemp retried 3x then dropped: '+str(sent))
check(ctl.home.get('Thermo2setTemp')=='25', 'optimistic setTemp')
# gap gating: packets arriving continuously block sending
fc.pub.clear(); ctl.on_ha_command(['commax','Light1','power','command'], 'OFF')
async def busy_bus():
    task = loop.create_task(ctl.sender())
    for _ in range(8):
        ctl.on_packet('B0010100000000B2'); await asyncio.sleep(0.005)
    task.cancel()
    try: await task
    except asyncio.CancelledError: pass
loop.run_until_complete(busy_bus())
check([p for t,p,_ in fc.pub if t=='ew11/send']==[], 'no send while bus busy (<15ms gaps)')
# HA restart -> republish
fc.pub.clear()
loop.run_until_complete(ctl.inbox.put(('homeassistant/status', b'online')))
async def consume_one():
    task = loop.create_task(ctl.consumer()); await asyncio.sleep(0.05); task.cancel()
    try: await task
    except asyncio.CancelledError: pass
loop.run_until_complete(consume_one())
check(any(t.startswith('homeassistant/') for t,_,_ in fc.pub) and any(t=='commax/Light3/power/state' for t,_,_ in fc.pub), 'HA online -> discovery + states republished')
# unknown header logged once, no exception
ctl.on_packet('3001000000000031'); ctl.on_packet('3002000000000032')
check('30' in ctl.unknown_seen, 'unknown header handled')
print('FAILS(1) =', fails)

# poll_wait: 마지막 패킷이 조회(헤더<0x80)면 40ms 전에는 송신하지 않음
fc.pub.clear(); ctl.queue.clear(); ctl.on_ha_command(['commax','Light2','power','command'], 'ON')
ctl.on_packet('3001000000000031')   # 조회
async def short_run():
    task = loop.create_task(ctl.sender()); await asyncio.sleep(0.025); task.cancel()
    try: await task
    except asyncio.CancelledError: pass
loop.run_until_complete(short_run())
check([p for t,p,_ in fc.pub if t=='ew11/send']==[], 'no send 25ms after a poll packet')
loop.run_until_complete(short_run())
check(len([p for t,p,_ in fc.pub if t=='ew11/send'])==1, 'send after poll_wait (40ms) elapsed')
fc.pub.clear(); ctl.queue.clear(); ctl.on_ha_command(['commax','Light2','power','command'], 'OFF')
ctl.on_packet('B0010100000000B2')   # 응답
loop.run_until_complete(short_run())
check(len([p for t,p,_ in fc.pub if t=='ew11/send'])==1, 'send 15ms after a response packet')
print('FAILS(2) =', fails)

# tx_enable=false : 큐에 넣어도 송신하지 않음 / max_tx_per_min 상한
fc.pub.clear(); ctl.queue.clear()
ctl.cfg['tx_enable'] = False
ctl.on_ha_command(['commax','Light1','power','command'], 'ON'); ctl.on_packet('B0010100000000B2')
loop.run_until_complete(short_run())
check([p for t,p,_ in fc.pub if t=='ew11/send']==[] and ctl.queue==[], 'tx_enable=false drops command without sending')
ctl.cfg['tx_enable'] = True; ctl.cfg['max_tx_per_min'] = 2; fc.pub.clear()
for n in (1,2,3):
    ctl.on_ha_command(['commax','Light%d'%n,'power','command'], 'ON')
ctl.on_packet('B0010100000000B2')
async def run_long():
    task = loop.create_task(ctl.sender()); await asyncio.sleep(0.8); task.cancel()
    try: await task
    except asyncio.CancelledError: pass
loop.run_until_complete(run_long())
check(len([p for t,p,_ in fc.pub if t=='ew11/send'])==2, 'max_tx_per_min caps sends at 2')
print('FAILS(3) =', fails)

# 환기 타이머 (2026-10-13 실측 형식)
fc.pub.clear(); ctl.queue.clear(); ctl.cfg['max_tx_per_min']=60; ctl.tx_times=[]
ctl.on_packet('F604010101011E1E')   # 켜짐, 약, 타이머 1h30m
st = {t[:-6]:p for t,p,_ in fc.pub if t.endswith('/state')}
check(st.get('commax/Fan1/timer_remaining')==b'90' and st.get('commax/Fan1/timer')==b'90', 'fan timer state 90 parsed '+str({k:v for k,v in st.items() if 'timer' in k}))
ctl.on_packet('F604010101011D1D')
st = {t[:-6]:p for t,p,_ in fc.pub if t.endswith('/state')}
check(st.get('commax/Fan1/timer_remaining')==b'89' and st.get('commax/Fan1/timer')==b'90', 'fan timer countdown 89 -> select stays 90')
ctl.on_packet('F6040101000000FC')
st = {t[:-6]:p for t,p,_ in fc.pub if t.endswith('/state')}
check(st.get('commax/Fan1/timer_remaining')==b'0' and st.get('commax/Fan1/timer')==b'off', 'fan timer off state')
ctl.on_ha_command(['commax','Fan1','timer','command'], '90')
check(ctl.queue and ctl.queue[-1]['send']=='780103011E00009B', 'timer 90 command 78 01 03 01 1E 00 00: '+str(ctl.queue[-1]['send'] if ctl.queue else None))
check(ctl.queue[-1]['ack'].match('F800010101011E1B') and ctl.queue[-1]['ack'].match('F604010101011E1E') and not ctl.queue[-1]['ack'].match('F800010101001E19') and not ctl.queue[-1]['ack'].match('F8000101000000FA'), 'timer 90 ack regex '+ctl.queue[-1]['ack'].pattern)
ctl.on_ha_command(['commax','Fan1','timer','command'], 'off')
check(ctl.queue[-1]['send']=='780103000000007C' and ctl.queue[-1]['ack'].match('F8000101000000FA') and ctl.queue[-1]['ack'].match('F6040101000000FC'), 'timer off command/ack')
ctl.on_ha_command(['commax','Fan1','timer','command'], '30')
check(ctl.queue[-1]['send']=='780103001E00009A', 'timer 30 command 78 01 03 00 1E')
fc.pub.clear(); ctl.publish_discovery()
topics=[t for t,_,_ in fc.pub]
check('homeassistant/select/commax_fan1_timer/config' in topics and 'homeassistant/sensor/commax_fan1_timer_remaining/config' in topics, 'timer discovery published')
print('FAILS(4) =', fails)
sys.exit(1 if fails else 0)
