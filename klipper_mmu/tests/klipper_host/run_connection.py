# SPDX-License-Identifier: GPL-3.0-or-later
"""Independent host-extra review using real Klipper public wrappers and parser.

Hardware/serial endpoint and reactor interleavings are modeled. Upstream selected
classes/functions are compiled unchanged from their AST nodes; no implementation
methods are replaced. Observer and explicit snapshot checks share this runner.
No hardware access, background jobs or product edits.
"""
from pathlib import Path
import argparse,ast,contextlib,hashlib,importlib.util,itertools,json,logging,secrets,sys,threading,types,unittest,zlib
from unittest.mock import patch
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--package',type=Path,default=Path(__file__).resolve().parents[2])
p.add_argument('--upstream',type=Path,required=True)
p.add_argument('--dictionary',type=Path,required=True)
p.add_argument('--results',type=Path,required=True)
a=p.parse_args()
REQUIRED_SHA256={
 'klippy/mcu.py':'36c47e1f910abfdd1670b323af752fa4a44afdf8ea21a936664f067b1e35c23b',
 'klippy/serialhdl.py':'1502e97ae0f941b79b3209f2bf518ead330e30ca8cfbbdf5fa7f6b9858575133',
 'klippy/msgproto.py':'c205bb7d70d968b5fbe8bbb86247a125994eb5d7acde16660afb2471672d6278',
 'klippy/klippy.py':'aa9eb47fbe3598fc814d40ed8e0b2b814a0af459fca7acab6732c59bf42e4659',
}
accepted={}
problems=[]
for rel,expect in REQUIRED_SHA256.items():
 path=a.upstream/rel
 got=hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else 'missing'
 if got!=expect:problems.append('%s: expected %s, got %s'%(rel,expect,got))
 else:accepted[rel]=got
if problems:
 sys.stderr.write('Upstream SHA256 mismatch for pinned APIs ce7002bedf37e938bb483572949f3703ac6476cb; refusing to load:\n'+'\n'.join(problems)+'\n')
 raise SystemExit(2)
a.results.mkdir(parents=True,exist_ok=False)
class CommandError(Exception):pass
class ConfigError(Exception):pass
class SerialError(Exception):pass
spec=importlib.util.spec_from_file_location('review_msgproto',a.upstream/'klippy/msgproto.py')
codec=importlib.util.module_from_spec(spec);spec.loader.exec_module(codec)
def selected(source,names,namespace):
 tree=ast.parse(source.read_text());nodes=[n for n in tree.body if isinstance(n,(ast.ClassDef,ast.FunctionDef)) and n.name in names]
 assert {n.name for n in nodes}==set(names)
 exec(compile(ast.Module(body=nodes,type_ignores=[]),str(source),'exec'),namespace)
serialns={'error':SerialError}
selected(a.upstream/'klippy/serialhdl.py',['SerialRetryCommand'],serialns)
upmcu=types.ModuleType('mcu');ns=upmcu.__dict__
ns.update(serialhdl=types.SimpleNamespace(error=SerialError,SerialRetryCommand=serialns['SerialRetryCommand']),msgproto=codec,zlib=zlib,logging=logging)
selected(a.upstream/'klippy/mcu.py',['error','DummyResponse','CommandQueryWrapper','CommandWrapper','AsyncResponseWrapper','MCUConfigHelper','MCU','get_printer_mcu'],ns)
sys.modules['mcu']=upmcu
# The actual upstream connect wrapper: Printer._connect/_set_state and their messages.
klippy_tree=ast.parse((a.upstream/'klippy/klippy.py').read_text())
klippy_nodes=[n for n in klippy_tree.body if isinstance(n,ast.Assign) and [getattr(t,'id',None) for t in n.targets] in (['message_ready'],['message_startup'],['message_restart'])]
klippy_nodes+=[n for c in klippy_tree.body if isinstance(c,ast.ClassDef) and c.name=='Printer' for n in c.body if isinstance(n,ast.FunctionDef) and n.name in ('_connect','_set_state')]
assert len(klippy_nodes)==5
klippyns={'logging':logging,'msgproto':codec,'mcu':upmcu,'pins':types.SimpleNamespace(error=type('PinError',(Exception,),{})),'util':types.SimpleNamespace(dump_mcu_build=lambda:None)}
exec(compile(ast.Module(body=klippy_nodes,type_ignores=[]),str(a.upstream/'klippy/klippy.py'),'exec'),klippyns)
spec=importlib.util.spec_from_file_location('review_extra',a.package/'host/__init__.py',submodule_search_locations=[str(a.package/'host')])
extra=importlib.util.module_from_spec(spec);sys.modules[spec.name]=extra;spec.loader.exec_module(extra)
dictionary=json.loads(a.dictionary.read_text())
class Reactor:
 def __init__(self):self.now=1.0;self.pauses=[]
 def monotonic(self):return self.now
 def pause(self,when):self.pauses.append(when);self.now=when
 def register_timer(self,*args):raise AssertionError('Observer must not add polling timers')
 def assert_no_pause(self):return contextlib.nullcontext()
 def mutex(self):return threading.Lock()
class Gcode:
 def __init__(self):self.commands={}
 def register_mux_command(self,command,key,value,fn,desc=None):
  assert (command,key,value) not in self.commands;self.commands[command,key,value]=fn
class Gcmd:
 def __init__(self):self.responses=[]
 def respond_info(self,text):self.responses.append(text)
 def error(self,text):return CommandError(text)
class Printer:
 command_error=CommandError;config_error=ConfigError
 def __init__(self):
  self.events={};self.trace=[];self.reactor=Reactor();self.gcode=Gcode();self.start_args={}
  resolver=types.SimpleNamespace(update_command=lambda s:s)
  self.objects={'gcode':self.gcode,'pins':types.SimpleNamespace(register_chip=lambda *args:None,get_pin_resolver=lambda n:resolver),
   'motion_queuing':types.SimpleNamespace(setup_mcu_movequeue=lambda *args:self.trace.append('movequeue_ready'))}
 def get_reactor(self):return self.reactor
 def lookup_object(self,name,*default):
  self.trace.append('lookup:'+name)
  if name in self.objects:return self.objects[name]
  if default:return default[0]
  raise ConfigError(name)
 def lookup_objects(self,module=None):return [(n,o) for n,o in self.objects.items() if module is None or n==module or n.startswith(module+' ')]
 def register_event_handler(self,event,cb):self.trace.append('register:'+event);self.events.setdefault(event,[]).append(cb)
 def get_start_args(self):return self.start_args
 def set_rollover_info(self,*args,**kwargs):pass
 def emit(self,event):
  for cb in list(self.events.get(event,[])):cb()
class UpstreamPrinter(Printer):
 """klippy:connect runs through the pinned upstream Printer._connect error classification."""
 _connect=klippyns['_connect'];_set_state=klippyns['_set_state']
 def __init__(self):super().__init__();self.event_handlers=self.events;self.state_message=klippyns['message_startup']
 def _read_config(self):pass
 def send_event(self,event,*params):self.trace.append('event:'+event)
 def invoke_shutdown(self,msg):raise AssertionError(msg)
 def upstream_connect(self):self._connect(self.reactor.now);return self.state_message
class Config:
 error=ConfigError
 def __init__(self,printer,name='klipper_mmu unit',mcu='mcu',diagnostic_startup=False):self.printer=printer;self.name=name;self.mcu=mcu;self.diagnostic_startup=diagnostic_startup
 def get_printer(self):return self.printer
 def get_name(self):return self.name
 def get(self,key,default=None):
  return self.mcu if key=='mcu' else default
 def getboolean(self,key,default=False):
  return self.diagnostic_startup if key=='diagnostic_startup' else default
class Serial:
 def __init__(self,printer):
  self.printer=printer;self.reactor=printer.reactor;self.parser=codec.MessageParser();self.parser.process_identify(json.dumps(dictionary).encode(),decompress=False)
  self.callbacks={};self.commands=[];self.config_commands=[];self.hook=None;self.omit=False;self.fail=False;self.change=None
  self.config={'is_config':0,'crc':0,'is_shutdown':0,'move_count':128}
  self.instance=0;self.state=0;self.last_query=0
 def get_msgparser(self):return self.parser
 def get_default_command_queue(self):return 'default'
 def get_serialqueue(self):return 'offline-queue'
 def register_response(self,callback,name,oid=None):
  if callback is None:self.callbacks.pop((name,oid),None)
  else:self.callbacks[name,oid]=callback
 def send(self,text):
  self.config_commands.append(text);self.printer.trace.append(text)
  if text.startswith('finalize_config crc='):self.config.update(is_config=1,crc=int(text.split('=')[1]))
 def raw_send(self,*args):self.raw_send_wait_ack(*args)
 def raw_send_wait_ack(self,data,*args):
  tag,pos=self.parser.msgid_parser.parse(data,0);fmt=self.parser.messages_by_id[tag];params,end=fmt.parse(data,0);assert end==len(data)
  self.commands.append((fmt.name,params));self.printer.trace.append(fmt.name)
  if fmt.name=='get_config':response='config';values=dict(self.config)
  else:
   assert fmt.name=='km_transport_query','Unexpected non-observation command'
   response='km_transport_state';values={'challenge':params['challenge'],'instance_hi':self.instance>>32,'instance_lo':self.instance&0xffffffff,'last_query':self.last_query,'state':self.state}
   if self.hook:
    hook=self.hook;self.hook=None;hook()
   if self.fail:raise SerialError('synthetic transport failure')
   if self.omit:return
   if self.change:values=self.change(values)
  # Use the actual identified response format for the normal wire roundtrip.
  rf=self.parser.messages_by_name[response];wire=rf.encode_by_name(**values);decoded,end=rf.parse(wire,0);assert end==len(wire)
  decoded.update({'#name':response,'#sent_time':self.reactor.now,'#receive_time':self.reactor.now})
  cb=self.callbacks.get((response,None))
  if cb:cb(decoded)
class Fixture:
 def __init__(self,seed=0,file_output=False,printer=None,mcu_name='mcu',name='unit',serial_type=Serial,diagnostic_startup=False):
  self.printer=printer or Printer();self.serial=serial_type(self.printer);self.file_output=file_output
  if file_output:self.printer.start_args['debugoutput']='synthetic-file-output'
  self.mcu=object.__new__(upmcu.MCU);self.mcu._printer=self.printer;self.mcu._name=mcu_name;self.mcu._serial=self.serial
  self.conn=types.SimpleNamespace(get_serial=lambda:self.serial,get_mcu=lambda:self.mcu,get_clocksync=lambda:None,is_fileoutput=lambda:self.file_output,
   is_shutdown=lambda:False,log_info=lambda:'',get_restart_helper=lambda:types.SimpleNamespace(check_restart_on_send_config=lambda:None,check_restart_on_crc_mismatch=lambda:None))
  self.mcu._conn_helper=self.conn
  self.helper=upmcu.MCUConfigHelper(types.SimpleNamespace(get_printer=lambda:self.printer),self.conn);self.mcu._config_helper=self.helper
  self.printer.objects['mcu' if mcu_name=='mcu' else 'mcu '+mcu_name]=self.mcu
  self.name=name
  with patch.object(secrets,'randbelow',return_value=seed):self.observer=extra.load_config_prefix(Config(self.printer,'klipper_mmu '+name,mcu_name,diagnostic_startup))
  self.printer.objects['klipper_mmu '+name]=self.observer
  self.observer_connect=self.printer.events['klippy:connect'][-1]
 def connect(self):self.printer.emit('klippy:connect')
 def query(self):
  cmd=Gcmd();self.printer.gcode.commands['KLIPPER_MMU_QUERY','MMU',self.name](cmd);return cmd
 def status(self):return self.observer.get_status(self.printer.reactor.now)
 def observations(self):return [p for n,p in self.serial.commands if n=='km_transport_query']
class HostChecks(unittest.TestCase):
 def test_advertised_host_watchdog_uses_this_mcu_dictionary(self):
  f=Fixture();f.connect()
  value=dictionary.get('config',{}).get('KLIPPER_MMU_HOST_LEASE_MS')
  expected=value if type(value) is int and 0<value<=60000 else None
  self.assertEqual(f.observer._firmware_host_lease_ms,expected)
  if expected is None:
   # Older saved dictionaries remain valid for observation but cannot enable
   # the managed lease without an actual bound from the MCU.
   self.assertFalse(f.status()['motion_available'])

 def test_actual_connect_order_crc_and_cached_status(self):
  f=Fixture();f.connect();s=f.status();self.assertEqual((s['connected'],s['state'],s['instance'],s['last_query'],s['motion_available']),(True,'unbound',0,0,False))
  self.assertLess(f.printer.trace.index('movequeue_ready'),f.printer.trace.index('km_transport_query'))
  commands=list(f.serial.config_commands);crc=f.helper._config_crc;wire=list(f.serial.commands)
  s['state']='tampered';self.assertEqual(f.status()['state'],'unbound');self.assertEqual(f.serial.commands,wire)
  json.dumps(f.status());f.query();self.assertEqual(f.serial.config_commands,commands);self.assertEqual(f.helper._config_crc,crc)
  self.assertEqual([x['challenge'] for x in f.observations()],[1,2]);self.assertTrue(f.query().responses)
 def test_all_observed_states_and_full_widths(self):
  for state,label,instance in [(0,'unbound',0),(1,'attached',2**64-1),(2,'shutdown',2**63+1),(3,'clock_unavailable',2**63+1)]:
   with self.subTest(state=state):
    f=Fixture();f.serial.state=state;f.serial.instance=instance;f.serial.last_query=0xffffffff if instance else 0;f.connect();s=f.status()
    self.assertEqual((s['state'],s['instance'],s['last_query'],s['motion_available']),(label,instance,f.serial.last_query,False))
 def test_file_output_is_not_live(self):
  f=Fixture(file_output=True);f.connect();s=f.status();self.assertEqual(s['state'],'file_output');self.assertFalse(s['connected']);self.assertIsNone(s['instance']);self.assertFalse(f.observations())
 def test_versions_and_actuation_validation(self):
  for key,value in [('KLIPPER_MMU_PROTOCOL_VERSION',None),('KLIPPER_MMU_PROTOCOL_VERSION',2),('KLIPPER_MMU_INTEGRATION_VERSION',2),('KLIPPER_MMU_ACTUATION',7),('KLIPPER_MMU_ACTUATION',None),('KLIPPER_MMU_ACTUATION',True),('KLIPPER_MMU_ACTUATION','0'),('KLIPPER_MMU_BOOT_IDENTITY_HOST',2),('KLIPPER_MMU_BOOT_IDENTITY_HOST',True),('KLIPPER_MMU_BOOT_IDENTITY_HOST','1')]:
   with self.subTest(key=key,value=value):
    # Incompatible firmware constants are a configuration error, not an internal error.
    f=Fixture(printer=UpstreamPrinter())
    if value is None:f.serial.parser.config.pop(key)
    else:f.serial.parser.config[key]=value
    state=f.printer.upstream_connect();s=f.status()
    self.assertTrue(s['last_error'].startswith('klipper_mmu: KLIPPER_MMU_'));self.assertTrue(state.startswith(s['last_error']+'\n'))
    self.assertIn('RESTART',state);self.assertNotIn('Internal error',state)
    self.assertFalse(f.observations());self.assertEqual((s['connected'],s['state'],s['instance']),(False,'unavailable',None))
 def test_temporary_observation_failure_completes_connect_and_retry_recovers(self):
  for serial_type,startup,mode in ((Serial,False,'omit'),(Serial,False,'fail'),(DiagnosticSerial,True,'diag_omit'),(DiagnosticSerial,True,'diag_fail')):
   with self.subTest(mode=mode):
    f=Fixture(printer=UpstreamPrinter(),serial_type=serial_type,diagnostic_startup=startup);setattr(f.serial,mode,True)
    f.serial.parser.config.update(KLIPPER_MMU_DEVICE_ID=1,KLIPPER_MMU_LANE_COUNT=2)
    self.assertIs(f.printer.upstream_connect(),klippyns['message_ready'])
    s=f.status();self.assertEqual((s['connected'],s['state'],s['instance'],s['last_query'],s['startup_state'],s['snapshot'],s['motion_available']),(False,'unavailable',None,None,None,None,False))
    self.assertTrue(s['last_error']);self.assertFalse(f.observer._mcu_lock.locked())
    self.assertEqual(s['firmware_composition'],(1,2))
    # A manual query still reports the failure; explicit retry recovers.
    with self.assertRaises(CommandError) as failed_query:f.query()
    self.assertEqual(str(failed_query.exception),s['last_error'])
    self.assertEqual(f.status()['state'],'unavailable')
    setattr(f.serial,mode,False);f.query();s=f.status()
    self.assertEqual((s['connected'],s['state'],s['instance'],s['last_error']),(True,'unbound',0,None))
    self.assertEqual(f.observations()[-1]['challenge'],3)
    self.assertEqual(s['firmware_composition'],(1,2))
    f.printer.emit('klippy:disconnect');self.assertIsNone(f.status()['firmware_composition'])
 def test_invalidation_during_connect_observation_is_not_overwritten(self):
  for event in ('klippy:disconnect','klippy:shutdown'):
   with self.subTest(event=event):
    f=Fixture();f.serial.hook=lambda:f.printer.emit(event);f.connect()
    s=f.status();self.assertEqual((s['connected'],s['state'],s['instance'],s['last_error']),(False,'disconnected',None,None))
    self.assertFalse(f.observer._mcu_lock.locked());self.assertFalse(f.serial.callbacks)
    f.observer_connect();self.assertTrue(f.status()['connected']);self.assertEqual([v['challenge'] for v in f.observations()],[1,2])
 def test_actuation_metadata_never_authorizes_motion(self):
  f=Fixture();f.serial.parser.config['KLIPPER_MMU_ACTUATION']=1;f.serial.instance=123;f.serial.state=1;f.connect()
  self.assertEqual(f.status()['firmware_actuation'],1);self.assertFalse(f.status()['motion_available'])
 def test_duplicate_mcu_refused_before_registration(self):
  f=Fixture();before={k:list(v) for k,v in f.printer.events.items()};commands=dict(f.printer.gcode.commands)
  with self.assertRaises(ConfigError):extra.load_config_prefix(Config(f.printer,'klipper_mmu another'))
  self.assertEqual(f.printer.events,before);self.assertEqual(f.printer.gcode.commands,commands)
 def test_separate_mcus_allowed(self):
  f=Fixture();g=Fixture(printer=f.printer,mcu_name='second',name='two');self.assertIsNot(f.observer.get_mcu(),g.observer.get_mcu())
 def test_wrong_challenge_and_inconsistent_state_invalidate(self):
  for change in [lambda v:{**v,'challenge':v['challenge']+1},lambda v:{**v,'state':1}]:
   f=Fixture();f.connect();f.serial.change=change
   with self.assertRaises(CommandError):f.query()
   self.assertIsNone(f.status()['instance']);self.assertIsNone(f.status()['last_query']);self.assertTrue(f.status()['last_error'])
 def test_timeout_and_wrapper_error(self):
  for mode in ('omit','fail'):
   f=Fixture();f.connect();setattr(f.serial,mode,True)
   with self.assertRaises(CommandError):f.query()
   self.assertIsNone(f.status()['instance']);self.assertTrue(f.status()['last_error']);self.assertLessEqual(len(f.observations()),7)
 def test_disconnect_and_shutdown_during_send(self):
  for event in ('klippy:disconnect','klippy:shutdown'):
   f=Fixture();f.connect();f.serial.hook=lambda:f.printer.emit(event)
   with self.assertRaises(CommandError):f.query()
   self.assertFalse(f.status()['connected']);self.assertIsNone(f.status()['instance'])
 def test_concurrent_query_waits_then_sends(self):
  f=Fixture();f.connect();other=threading.Thread(target=f.query)
  def concurrent():
   other.start();other.join(0.05);self.assertEqual(len(f.observations()),before+1)
  f.serial.hook=concurrent;before=len(f.observations());f.query();other.join(5)
  self.assertEqual([v['challenge'] for v in f.observations()],[1,2,3]);self.assertTrue(f.status()['connected'])
 def test_reconnect_preserves_challenge_counter_and_retained_report(self):
  f=Fixture();f.connect();f.printer.emit('klippy:disconnect');f.serial.state=1;f.serial.instance=0x1234567887654321;f.serial.last_query=0xfffffffe
  config=list(f.serial.config_commands);f.observer_connect();self.assertEqual(f.serial.config_commands,config)
  self.assertEqual([v['challenge'] for v in f.observations()],[1,2]);self.assertEqual(f.status()['last_query'],0xfffffffe)
 def test_challenge_wraps_to_one_and_rejects_pre_wrap_echo(self):
  f=Fixture(seed=0xfffffffe);f.connect();f.query();self.assertTrue(f.status()['connected'])
  f.serial.change=lambda v:{**v,'challenge':0xffffffff}
  with self.assertRaisesRegex(CommandError,'not echoed'):f.query()
  self.assertEqual([v['challenge'] for v in f.observations()],[0xffffffff,1,2]);self.assertIsNone(f.status()['instance'])
 def test_reconnect_waits_and_old_send_cannot_downgrade_new_generation(self):
  f=Fixture();f.connect();other=threading.Thread(target=f.observer_connect)
  def reconnect():
   f.printer.emit('klippy:disconnect');other.start();other.join(0.05)
   self.assertEqual(len(f.observations()),before+1)
  f.serial.hook=reconnect;before=len(f.observations())
  with self.assertRaisesRegex(CommandError,'stale transport response'):f.query()
  other.join(5);s=f.status()
  self.assertEqual((s['connected'],s['state'],s['last_error']),(True,'unbound',None))
  self.assertEqual(len(f.observations()),before+2);self.assertFalse(f.serial.callbacks)
 def test_reconnect_uses_current_dictionary(self):
  f=Fixture();f.connect();f.printer.emit('klippy:disconnect')
  # Model a reidentified transport on this object. Real SerialReader replaces
  # its MessageParser in _start_session; wrappers retain their encoded format.
  changed=json.loads(json.dumps(dictionary));command='km_transport_query challenge=%u'
  tags=set(changed['commands'].values())|set(changed['responses'].values())
  changed['commands'][command]=max(tags)+1
  f.serial.parser=codec.MessageParser();f.serial.parser.process_identify(json.dumps(changed).encode(),decompress=False)
  f.observer_connect();self.assertEqual(len(f.observations()),2)
  self.assertTrue(f.status()['connected']);self.assertEqual(f.status()['state'],'unbound')
 def test_lookup_failure_is_reported_and_explicit_retry_can_recover(self):
  f=Fixture();original=f.serial.parser
  changed=json.loads(json.dumps(dictionary));changed['commands'].pop('km_transport_query challenge=%u')
  f.serial.parser=codec.MessageParser();f.serial.parser.process_identify(json.dumps(changed).encode(),decompress=False)
  f.connect();s=f.status()
  self.assertEqual((s['connected'],s['state'],s['instance']),(False,'unavailable',None));self.assertIn('km_transport_query',s['last_error'])
  self.assertFalse(f.observations());f.serial.parser=original;f.query()
  self.assertTrue(f.status()['connected'])
 def test_inflight_invalidation_releases_guard_after_unwind(self):
  for event in ('klippy:disconnect','klippy:shutdown'):
   with self.subTest(event=event):
    f=Fixture();f.connect();f.serial.hook=lambda:f.printer.emit(event)
    with self.assertRaises(CommandError):f.query()
    self.assertFalse(f.serial.callbacks)
    f.observer_connect();self.assertTrue(f.status()['connected'])
    f.query();self.assertEqual([v['challenge'] for v in f.observations()],[1,2,3,4])
class DiagnosticSerial(Serial):
 """Real parser round-trip for km_diagnostic_query; every other frame stays on Serial."""
 def __init__(self,printer):
  super().__init__(printer)
  self.startup_state=0;self.startup_result=-3;self.diag_queries=[]
  self.diag_hook=None;self.diag_omit=False;self.diag_fail=False;self.diag_change=None
 def raw_send_wait_ack(self,data,*args):
  tag,pos=self.parser.msgid_parser.parse(data,0);fmt=self.parser.messages_by_id[tag]
  if fmt.name!='km_diagnostic_query':return super().raw_send_wait_ack(data,*args)
  params,end=fmt.parse(data,0);assert end==len(data)
  self.commands.append((fmt.name,params));self.printer.trace.append(fmt.name);self.diag_queries.append(dict(params))
  if self.diag_hook:
   hook=self.diag_hook;self.diag_hook=None;hook()
  if self.diag_fail:raise SerialError('synthetic diagnostic failure')
  if self.diag_omit:return
  values={'challenge':params['challenge'],'state':self.startup_state,'result':self.startup_result}
  if self.diag_change:values=self.diag_change(dict(values))
  response='km_diagnostic_state';rf=self.parser.messages_by_name[response]
  wire=rf.encode_by_name(**values);decoded,end=rf.parse(wire,0);assert end==len(wire)
  decoded.update({'#name':response,'#sent_time':self.reactor.now,'#receive_time':self.reactor.now})
  cb=self.callbacks.get((response,None))
  if cb:cb(decoded)
HOST_EPOCH_FORMAT='km_diagnostic_start_host epoch_hi=%u epoch_lo=%u'
class HostEpochSerial(DiagnosticSerial):
 """Host-epoch dictionary plus a modeled firmware latch: first nonzero epoch binds for the MCU lifetime."""
 def __init__(self,printer):
  super().__init__(printer)
  d=json.loads(json.dumps(dictionary));d.setdefault('config',{})['KLIPPER_MMU_BOOT_IDENTITY_HOST']=1
  d['commands'].pop('km_diagnostic_start',None)
  d['commands'][HOST_EPOCH_FORMAT]=max(set(d['commands'].values())|set(d['responses'].values()))+1
  self.parser=codec.MessageParser();self.parser.process_identify(json.dumps(d).encode(),decompress=False)
  self.host_epochs=[];self.bound=False
 def raw_send(self,data,*args):
  tag,pos=self.parser.msgid_parser.parse(data,0);fmt=self.parser.messages_by_id[tag]
  if fmt.name!='km_diagnostic_start_host':return super().raw_send(data,*args)
  params,end=fmt.parse(data,0);assert end==len(data)
  epoch=(params['epoch_hi']<<32)|params['epoch_lo']
  self.commands.append((fmt.name,params));self.printer.trace.append(fmt.name);self.host_epochs.append(epoch)
  if not self.bound and self.startup_state==0 and epoch:
   self.bound=True;self.instance=epoch;self.state=1;self.startup_state=2;self.startup_result=0
def reidentify(serial,updates):
 for key,value in updates.items():
  if value is None:serial.parser.config.pop(key,None)
  else:serial.parser.config[key]=value
class DiagnosticChecks(unittest.TestCase):
 def diag(self,**kwargs):
  kwargs.setdefault('serial_type',DiagnosticSerial);kwargs.setdefault('diagnostic_startup',True);return Fixture(**kwargs)
 def test_default_sends_neither_start_nor_query(self):
  f=Fixture(serial_type=DiagnosticSerial);f.connect();status=f.status()
  self.assertFalse(status['diagnostic_startup']);self.assertIsNone(status['startup_state']);self.assertIsNone(status['startup_result'])
  self.assertFalse(status['motion_available']);self.assertNotIn('km_diagnostic_start',f.serial.config_commands);self.assertFalse(f.serial.diag_queries)
  sent=len(f.serial.commands);f.status();self.assertEqual(len(f.serial.commands),sent)
 def test_opt_in_orders_init_and_query_pair(self):
  f=self.diag();f.connect();cmds=f.serial.config_commands;trace=f.printer.trace
  self.assertEqual(cmds.count('km_diagnostic_start'),1)
  start=trace.index('km_diagnostic_start')
  self.assertTrue(any(item.startswith('finalize_config crc=') for item in trace[:start]))
  query_at=trace.index('km_transport_query')
  self.assertIn('get_config',trace[start+1:query_at]);self.assertLess(start,trace.index('km_diagnostic_query'))
  self.assertEqual(f.observations()[0]['challenge'],f.serial.diag_queries[0]['challenge'])
  sent=len(f.serial.commands);status=f.status();self.assertEqual(len(f.serial.commands),sent)
  self.assertEqual(status['startup_state'],'idle');self.assertEqual(status['startup_result'],-3);self.assertFalse(status['motion_available'])
  reply=f.query();self.assertEqual(len(f.observations()),2);self.assertEqual(len(f.serial.diag_queries),2)
  self.assertEqual(f.observations()[1]['challenge'],f.serial.diag_queries[1]['challenge'])
  self.assertEqual(cmds.count('km_diagnostic_start'),1)
  text=reply.responses[-1]
  self.assertIn('startup_state=idle',text);self.assertIn('startup_result=-3',text);self.assertIn('motion_available=False',text);self.assertNotIn('km_diagnostic_start',f.serial.commands[-2][0])
 def test_startup_result_matrix_never_authorizes_motion(self):
  rows=[(0,-3,'idle',0),(1,-3,'waiting',0),(2,0,'ready',1),(3,-1,'faulted',1)]+[(3,result,'faulted',0) for result in (-2,-4,-5,-6,-7,-8,-9,-10)]
  for code,result,name,transport in rows:
   with self.subTest(state=name,result=result,transport=transport):
    f=self.diag();f.serial.state=transport;f.serial.instance=(2**64-1) if transport==1 else 0;f.serial.startup_state=code;f.serial.startup_result=result;f.connect();status=f.status()
    self.assertEqual(status['startup_state'],name);self.assertEqual(status['startup_result'],result);self.assertFalse(status['motion_available'])
    self.assertEqual((status['state'],status['instance'],status['last_query']),('attached',2**64-1,0) if transport==1 else ('unbound',0,0))
    if name!='ready':self.assertNotEqual(status['startup_state'],'ready')
    self.assertTrue(status['connected'])
 def test_malformed_startup_clears_observation(self):
  f=self.diag()
  # %c/%i on the wire normalize to ints. Bool, missing, and non-int checks call _decode_startup directly.
  challenge=1
  direct=(
   {'challenge':challenge,'state':True,'result':-3},
   {'challenge':challenge,'result':-3},
   {'challenge':challenge,'state':'2','result':0},
   {'challenge':challenge,'state':2,'result':False},
   {'challenge':challenge,'state':0,'result':0},
   {'challenge':challenge,'state':3,'result':-3},
  )
  for params in direct:
   with self.subTest(direct=params):
    with self.assertRaises(CommandError):f.observer._decode_startup(params,challenge)
  self.assertEqual(f.observer._decode_startup({'challenge':challenge,'state':2,'result':0},challenge),('ready',0))
  self.assertEqual(f.observer._decode_startup({'challenge':challenge,'state':2,'result':0,'extra':True},challenge),('ready',0))
  self.assertEqual(f.observer._decode_startup({'challenge':challenge,'state':3,'result':-10},challenge),('faulted',-10))
  wires=(
   lambda v:dict(v,state=4),
   lambda v:dict(v,state=2,result=-3),
   lambda v:dict(v,state=3,result=-3),
   lambda v:dict(v,state=3,result=-11),
   lambda v:dict(v,state=0,result=0),
   lambda v:dict(v,challenge=0),
  )
  for change in wires:
   with self.subTest(change=change):
    trial=self.diag();trial.connect();trial.observer._snapshot={'instance':1,'query':1};trial.serial.diag_change=change
    with self.assertRaises(CommandError):trial.query()
    status=trial.status()
    self.assertIsNone(status['instance']);self.assertIsNone(status['last_query']);self.assertIsNone(status['startup_state']);self.assertIsNone(status['startup_result']);self.assertIsNone(status['snapshot'])
    self.assertFalse(status['connected']);self.assertTrue(status['last_error'])
 def test_config_callback_rejects_gate_before_init(self):
  cases=(
   {'KLIPPER_MMU_DIAGNOSTIC_STARTUP':None},
   {'KLIPPER_MMU_DIAGNOSTIC_STARTUP':0},
   {'KLIPPER_MMU_DIAGNOSTIC_STARTUP':2},
   {'KLIPPER_MMU_DIAGNOSTIC_STARTUP':True},
   {'KLIPPER_MMU_DIAGNOSTIC_STARTUP':'pending'},
   {'KLIPPER_MMU_PROTOCOL_VERSION':2},
   {'KLIPPER_MMU_INTEGRATION_VERSION':0},
  )
  for updates in cases:
   with self.subTest(updates=updates):
    f=self.diag();reidentify(f.serial,updates)
    with self.assertRaises(ConfigError):f.connect()
    self.assertNotIn('km_diagnostic_start',f.serial.config_commands);self.assertFalse(f.serial.diag_queries)
    self.assertFalse(any(name=='km_transport_query' for name,params in f.serial.commands))
 def test_file_output_opt_in_sends_nothing(self):
  f=self.diag(file_output=True);f.connect();status=f.status()
  self.assertEqual(status['state'],'file_output');self.assertTrue(status['diagnostic_startup']);self.assertIsNone(status['startup_state']);self.assertFalse(status['motion_available'])
  self.assertNotIn('km_diagnostic_start',f.serial.config_commands);self.assertFalse(f.serial.diag_queries)
 def test_diagnostic_failure_invalidates_and_query_recovers(self):
  for mode in ('timeout','error'):
   with self.subTest(mode=mode):
    f=self.diag();f.connect();self.assertTrue(f.status()['connected']);before=len(f.serial.diag_queries)
    if mode=='timeout':f.serial.diag_omit=True
    else:f.serial.diag_fail=True
    with self.assertRaises(CommandError):f.query()
    self.assertFalse(f.observer._mcu_lock.locked());status=f.status()
    self.assertFalse(status['connected']);self.assertIsNone(status['instance']);self.assertIsNone(status['last_query']);self.assertIsNone(status['startup_state']);self.assertIsNone(status['startup_result']);self.assertIsNone(status['snapshot'])
    if mode=='timeout':
     self.assertFalse(f.serial.callbacks)
     self.assertGreater(len(f.serial.diag_queries),before);self.assertLess(len(f.serial.diag_queries),30);self.assertTrue(f.printer.reactor.pauses)
    else:
     self.assertEqual(list(f.serial.callbacks),[('km_diagnostic_state',None)])
    f.serial.diag_omit=False;f.serial.diag_fail=False;f.query()
    self.assertFalse(f.serial.callbacks)
    self.assertTrue(f.status()['connected']);self.assertEqual(f.status()['startup_state'],'idle');self.assertEqual(f.serial.config_commands.count('km_diagnostic_start'),1)
    self.assertEqual([item['challenge'] for item in f.observations()],[1,2,3])
    self.assertEqual(f.serial.diag_queries[0]['challenge'],1)
    self.assertTrue(all(item['challenge']==2 for item in f.serial.diag_queries[1:-1]))
    self.assertEqual(f.serial.diag_queries[-1]['challenge'],3)
 def test_invalidate_during_diagnostic_query_preserves_fresh_generation(self):
  for event in ('klippy:disconnect','klippy:shutdown'):
   with self.subTest(event=event):
    f=self.diag();f.connect();seen={};starts=f.serial.config_commands.count('km_diagnostic_start');finalizes=sum(item.startswith('finalize_config') for item in f.serial.config_commands)
    def later():
     # Concurrent callers wait for the in-flight observation to unwind.
     seen['cached']=f.query().responses[-1];f.observer_connect();seen['fresh']=f.status()
    other=threading.Thread(target=later)
    def during():
     seen['entry']=(f.observer._generation,f.observer._challenge,f.observer._mcu_lock.locked(),f.observer._active,len(f.observations()),len(f.serial.diag_queries))
     f.printer.emit(event);status=f.status()
     seen['event']=(f.observer._generation,f.observer._active,status['connected'],status['instance'],status['last_query'],status['startup_state'],status['startup_result'],status['snapshot'])
     other.start();other.join(0.05);seen['waiting_io']=(len(f.observations()),len(f.serial.diag_queries))
    f.serial.diag_hook=during
    with self.assertRaisesRegex(CommandError,'stale transport response'):f.query()
    other.join(5)
    self.assertEqual(seen['entry'],(1,2,True,True,2,2))
    self.assertEqual(seen['event'],(2,False,False,None,None,None,None,None));self.assertEqual(seen['waiting_io'],(2,2))
    self.assertIn('connected=False',seen['cached'])
    fresh=seen['fresh'];self.assertEqual((fresh['connected'],fresh['startup_state'],fresh['last_error']),(True,'idle',None))
    self.assertFalse(f.observer._mcu_lock.locked());self.assertFalse(f.serial.callbacks)
    self.assertEqual((f.observer._generation,f.observer._challenge),(3,3))
    self.assertEqual([item['challenge'] for item in f.observations()],[1,2,3]);self.assertEqual([item['challenge'] for item in f.serial.diag_queries],[1,2,3])
    self.assertEqual(f.serial.config_commands.count('km_diagnostic_start'),starts);self.assertEqual(sum(item.startswith('finalize_config') for item in f.serial.config_commands),finalizes)
 def test_reidentify_drops_startup_wrapper_without_rerunning_helper(self):
  f=self.diag();f.connect();parser=f.serial.parser;wrapper=f.observer._startup_query;finalizes=sum(item.startswith('finalize_config') for item in f.serial.config_commands);starts=f.serial.config_commands.count('km_diagnostic_start')
  f.query();self.assertIs(f.serial.parser,parser)
  f.serial.diag_fail=True
  with self.assertRaises(CommandError):f.query()
  f.serial.diag_fail=False;self.assertIs(f.serial.parser,parser);self.assertIs(f.observer._startup_query,wrapper)
  f.printer.emit('klippy:disconnect');self.assertIsNone(f.observer._startup_query);f.observer_connect();self.assertIs(f.serial.parser,parser)
  self.assertIsNot(f.observer._startup_query,wrapper);self.assertIsNotNone(f.observer._startup_query)
  self.assertEqual(sum(item.startswith('finalize_config') for item in f.serial.config_commands),finalizes);self.assertEqual(f.serial.config_commands.count('km_diagnostic_start'),starts)
  changed=json.loads(json.dumps(dictionary));command='km_diagnostic_query challenge=%u'
  tags=set(changed['commands'].values())|set(changed['responses'].values());changed['commands'][command]=max(tags)+1
  f.printer.emit('klippy:disconnect');self.assertIsNone(f.observer._startup_query)
  f.serial.parser=codec.MessageParser();f.serial.parser.process_identify(json.dumps(changed).encode(),decompress=False)
  f.observer_connect();self.assertTrue(f.status()['connected']);self.assertEqual(f.status()['startup_state'],'idle')
  self.assertEqual(f.serial.config_commands.count('km_diagnostic_start'),starts)
  self.assertEqual(f.serial.diag_queries[-1]['challenge'],f.observations()[-1]['challenge'])
 def test_matching_crc_init_on_already_configured_mcu(self):
  # The MCU owns latch idempotence. This host check only orders init on an already-configured MCU.
  probe=self.diag();probe.connect();crc=probe.serial.config['crc'];self.assertNotEqual(crc,0)
  self.assertEqual(probe.serial.config_commands.count('km_diagnostic_start'),1)
  f=self.diag();f.serial.config.update(is_config=1,crc=crc);f.connect()
  self.assertEqual(f.serial.config_commands.count('km_diagnostic_start'),1)
  self.assertFalse(any(item.startswith('finalize_config') for item in f.serial.config_commands))
  trace=f.printer.trace;start=trace.index('km_diagnostic_start')
  self.assertLess(start,trace.index('km_transport_query'));self.assertLess(start,trace.index('km_diagnostic_query'))
  self.assertEqual(f.observations()[0]['challenge'],f.serial.diag_queries[0]['challenge']);self.assertFalse(f.status()['motion_available'])
 def host_epoch(self):
  f=self.diag(serial_type=HostEpochSerial)
  f.helper.register_post_init_callback(lambda:f.printer.trace.append('mcu_post_init'));return f
 def test_host_epoch_follows_config_and_is_fresh_per_connect(self):
  f=self.host_epoch()
  with patch.object(secrets,'randbelow',side_effect=[0x123456789abc,(1<<64)-2]):
   f.connect();trace=f.printer.trace;start=trace.index('km_diagnostic_start_host')
   self.assertNotIn('km_diagnostic_start',f.serial.config_commands)
   self.assertLess(trace.index('mcu_post_init'),start)
   self.assertLess(max(i for i,item in enumerate(trace) if item=='get_config'),start)
   self.assertLess(start,trace.index('km_transport_query'));self.assertLess(start,trace.index('km_diagnostic_query'))
   first=0x123456789abd;self.assertEqual(f.serial.host_epochs,[first])
   self.assertEqual((f.status()['instance'],f.status()['startup_state']),(first,'ready'))
   self.assertEqual(f.observations()[-1]['challenge'],f.serial.diag_queries[-1]['challenge'])
   # Same observer, new connect: a fresh full-width epoch is sent; the bound MCU ignores it.
   f.printer.emit('klippy:disconnect');f.observer_connect()
  self.assertEqual(f.serial.host_epochs,[first,(1<<64)-1]);self.assertEqual(f.status()['instance'],first)
  self.assertEqual(f.serial.config_commands.count('km_diagnostic_start'),0);self.assertFalse(f.status()['motion_available'])
 def test_host_epoch_restart_keeps_ready_mcu_instance(self):
  probe=self.host_epoch();probe.connect();crc=probe.serial.config['crc']
  f=self.host_epoch();old=0xfeedface01234567
  f.serial.config.update(is_config=1,crc=crc);f.serial.bound=True;f.serial.instance=old;f.serial.state=1;f.serial.startup_state=2;f.serial.startup_result=0
  with patch.object(secrets,'randbelow',return_value=0x102030405060):f.connect()
  self.assertEqual(f.serial.host_epochs,[0x102030405061]);self.assertEqual(f.status()['instance'],old)
  self.assertFalse(any(item.startswith('finalize_config') for item in f.serial.config_commands))
 def test_host_epoch_constant_must_be_integer_bit(self):
  for value in (2,True,'1'):
   with self.subTest(value=value):
    f=self.host_epoch();reidentify(f.serial,{'KLIPPER_MMU_BOOT_IDENTITY_HOST':value})
    with self.assertRaises(ConfigError):f.connect()
    self.assertFalse(f.serial.host_epochs);self.assertNotIn('km_diagnostic_start',f.serial.config_commands)
 def test_snapshot_preflight_shares_observe(self):
  f=self.diag();f.connect();before=len(f.serial.diag_queries)
  with self.assertRaises(CommandError):f.printer.gcode.commands['KLIPPER_MMU_SNAPSHOT','MMU',f.name](Gcmd())
  self.assertEqual(len(f.serial.diag_queries),before+1);self.assertFalse(f.observer._mcu_lock.locked())
  other=threading.Thread(target=f.query);f.serial.diag_hook=other.start
  with self.assertRaises(CommandError):f.printer.gcode.commands['KLIPPER_MMU_SNAPSHOT','MMU',f.name](Gcmd())
  other.join(5);self.assertFalse(f.serial.callbacks);self.assertTrue(f.status()['connected']);self.assertEqual(f.status()['startup_state'],'idle')
class Completion:
 def __init__(self, reactor):self.reactor=reactor;self.done=False;self.value=None
 def complete(self, value):
  assert self.reactor.phase=='reactor', 'Completion mutated on serial thread'
  assert not self.done, 'Completion overwritten'
  self.done=True;self.value=value
 def test(self):return self.done
 def wait(self, deadline, waketime_result=None):
  self.reactor.waits.append(deadline)
  while not self.done and self.reactor.queued:
   when, cb=self.reactor.queued.pop(0)
   if when>deadline:
    self.reactor.queued.insert(0,(when,cb));break
   self.reactor.now=max(self.reactor.now,when);cb(self.reactor.now)
  if not self.done:self.reactor.now=deadline
  return self.value if self.done else waketime_result
class SnapshotReactor(Reactor):
 def __init__(self):super().__init__();self.queued=[];self.waits=[];self.phase='reactor';self.completions=[]
 def completion(self):
  c=Completion(self);self.completions.append(c);return c
 def register_async_callback(self,callback,waketime=None):
  self.queued.append((self.now if waketime is None else waketime,callback))
  self.queued.sort(key=lambda x:x[0])
 def drain(self):
  while self.queued:
   when,cb=self.queued.pop(0);self.now=max(self.now,when);cb(self.now)
class SnapshotSerial(Serial):
 def __init__(self,printer):
  super().__init__(printer);self.order=list(range(5));self.parts=[];self.transform=None
  self.before_parts=None;self.response_error=None;self.reject_registration=None
  self.fail_part=None;self.saved_callbacks=[];self.direct=False
 def register_response(self,callback,name,oid=None):
  if callback is not None and name==self.reject_registration:
   raise SerialError('registration rejected by fixture')
  super().register_response(callback,name,oid)
 def emit(self,name,values):
  cb=self.callbacks.get((name,None))
  if cb is None:return
  if not self.direct:
   fmt=self.parser.messages_by_name[name];wire=fmt.encode_by_name(**values)
   values,end=fmt.parse(wire,0);assert end==len(wire)
  values.update({'#name':name,'#sent_time':self.reactor.now,'#receive_time':self.reactor.now})
  self.saved_callbacks.append((cb,dict(values)))
  self.reactor.phase='serial'
  try:cb(values)
  finally:self.reactor.phase='reactor'
 def raw_send_wait_ack(self,data,*args):
  tag,pos=self.parser.msgid_parser.parse(data,0);fmt=self.parser.messages_by_id[tag]
  if fmt.name!='km_snapshot_part':return super().raw_send_wait_ack(data,*args)
  params,end=fmt.parse(data,0);assert end==len(data)
  self.commands.append((fmt.name,params));self.parts.append(params)
  part=params['part']
  if part==self.fail_part:raise SerialError('snapshot send failure')
  if part!=4:return
  if self.before_parts:self.before_parts()
  if self.response_error is not None:
   self.emit('km_command_result',dict(instance_hi=0,instance_lo=0,query=params['query'],command=8,result=self.response_error))
   return
  hi=params['instance_hi'];lo=params['instance_lo'];q=params['query']
  self.last_query=q
  for n in self.order:
   name=sys.modules['review_extra.protocol'].STATUS_MESSAGES[n];rf=self.parser.messages_by_name[name]
   values={key:0 for key,t in rf.param_names};values.update(instance_hi=hi,instance_lo=lo,query=q)
   if n==0:values.update(state=3,flags=511,session=0xffffffff,request=0x87654321)
   if n==1:values.update(position_hi=0xffffffff,position_lo=0xfffffffe,velocity_bits=0xfffffffc,follower_state=1,follower_direction=1)
   if n==2:values.update(control_ms=77,control_us=77001,acquired_ms=78,acquired_us=78002)
   if n in (3,4):values.update(client0=1,client1=0xffffffff,client2=2,client3=3)
   if self.transform:values=self.transform(n,values)
   if values is not None:self.emit(name,values)
class SnapshotFixture(Fixture):
 def __init__(self,seed=0,file_output=False):
  printer=Printer();printer.reactor=SnapshotReactor()
  super().__init__(seed=seed,file_output=file_output,printer=printer,serial_type=SnapshotSerial)
  self.serial.instance=0xfedcba9812345678;self.serial.state=1
 def snapshot(self):
  gcmd=Gcmd();self.printer.gcode.commands['KLIPPER_MMU_SNAPSHOT','MMU',self.name](gcmd);return gcmd
class SnapshotChecks(unittest.TestCase):
 def ready(self):
  f=SnapshotFixture();f.connect();return f
 def assert_clean(self,f):
  self.assertFalse(f.serial.callbacks);self.assertFalse(f.observer._mcu_lock.locked())
 def test_all_part_orders_and_cached_copy(self):
  for order in itertools.permutations(range(5)):
   with self.subTest(order=order):
    f=self.ready();f.serial.last_query=0x12345678;f.serial.order=order;f.snapshot()
    status=f.status();s=status['snapshot'];self.assertEqual(s['instance'],f.serial.instance)
    self.assertEqual(s['query'],0x12345679);self.assertEqual(s['position'],-2)
    self.assertEqual(s['velocity'],-4);self.assertEqual(s['client_sessions'],(1,0xffffffff,2,3))
    self.assertEqual(s['acquired_us'],78002);self.assertFalse(status['motion_available'])
    self.assertEqual((s['follower_state'],s['follower_direction'],s['follower_fault']),(1,1,0))
    before=len(f.serial.commands);s['position']=999;self.assertEqual(f.status()['snapshot']['position'],-2)
    self.assertEqual(before,len(f.serial.commands));self.assertEqual([x['part'] for x in f.serial.parts],list(range(5)))
    self.assertEqual(len(f.printer.reactor.waits),1);self.assert_clean(f)
 def test_inactive_file_output_and_unbound_do_not_request_parts(self):
  for kind in ('inactive','file','unbound','shutdown','clock'):
   with self.subTest(kind=kind):
    f=SnapshotFixture(file_output=kind=='file')
    if kind!='inactive':f.connect()
    if kind in ('unbound','shutdown','clock'):
     f.serial.state={'unbound':0,'shutdown':2,'clock':3}[kind]
     if kind=='unbound':f.serial.instance=0
    with self.assertRaises(CommandError):f.snapshot()
    self.assertFalse(f.serial.parts);self.assertIsNone(f.status()['snapshot']);self.assert_clean(f)
 def test_incomplete_uses_one_timeout_then_can_recover(self):
  f=self.ready();f.serial.order=[0,1,2,3];start=f.printer.reactor.now
  with self.assertRaises(CommandError):f.snapshot()
  self.assertLessEqual(f.printer.reactor.now-start,5.001);self.assertIsNone(f.status()['snapshot']);self.assert_clean(f)
  oldq=f.serial.parts[0]['query'];f.serial.order=list(range(5));f.snapshot()
  self.assertGreater(f.status()['snapshot']['query'],oldq);self.assert_clean(f)
 def test_wrong_identity_or_query_never_assembles(self):
  for field in ('instance_hi','query'):
   with self.subTest(field=field):
    f=self.ready();f.serial.transform=lambda n,v:{**v,field:v[field]^1} if n==3 else v
    with self.assertRaises(CommandError):f.snapshot()
    self.assertIsNone(f.status()['snapshot']);self.assert_clean(f)
 def test_duplicate_and_conflicting_fragments(self):
  f=self.ready();f.serial.order=[0,0,1,2,3,4];f.snapshot();self.assert_clean(f)
  f=self.ready();f.serial.order=[0,0,1,2,3,4];count=[0]
  def change(n,v):
   if n==0:count[0]+=1;v['session']=count[0]
   return v
  f.serial.transform=change
  with self.assertRaises(CommandError):f.snapshot()
  self.assertIsNone(f.status()['snapshot']);self.assert_clean(f)
 def test_matching_error_with_changed_instance_is_immediate(self):
  for code in range(1,8):
   with self.subTest(code=code):
    f=self.ready();f.serial.response_error=code;start=f.printer.reactor.now
    with self.assertRaises(CommandError):f.snapshot()
    self.assertLess(f.printer.reactor.now-start,1);self.assert_clean(f)
 def test_admin_ok_does_not_complete_snapshot(self):
  f=self.ready();f.serial.response_error=0
  with self.assertRaises(CommandError):f.snapshot()
  self.assertIsNone(f.status()['snapshot']);self.assert_clean(f)
 def test_highwater_and_exhaustion_survive_reconnect(self):
  f=self.ready();f.serial.last_query=0xfffffffd;f.snapshot();self.assertEqual(f.status()['snapshot']['query'],0xfffffffe)
  f.printer.emit('klippy:disconnect');f.serial.last_query=0;f.serial.instance^=1;f.observer_connect();f.snapshot()
  self.assertEqual(f.status()['snapshot']['query'],0xffffffff);count=len(f.serial.parts)
  with self.assertRaises(CommandError):f.snapshot()
  self.assertEqual(len(f.serial.parts),count);self.assertIsNone(f.status()['snapshot']);self.assert_clean(f)
 def test_registration_and_send_failure_cleanup(self):
  for mode in ('registration','lookup','send'):
   with self.subTest(mode=mode):
    f=self.ready()
    if mode=='registration':f.serial.reject_registration='km_status_time'
    if mode=='send':f.serial.fail_part=2
    original=f.serial.parser
    if mode=='lookup':
     d=json.loads(json.dumps(dictionary));d['commands'].pop('km_snapshot_part instance_hi=%u instance_lo=%u query=%u part=%c')
     f.serial.parser=codec.MessageParser();f.serial.parser.process_identify(json.dumps(d).encode(),decompress=False)
    with self.assertRaises(CommandError):f.snapshot()
    self.assert_clean(f);self.assertIsNone(f.status()['snapshot'])
    f.serial.reject_registration=None;f.serial.fail_part=None;f.serial.parser=original;f.snapshot();self.assert_clean(f)
 def test_disconnect_and_shutdown_during_pending_snapshot(self):
  for event in ('klippy:disconnect','klippy:shutdown'):
   with self.subTest(event=event):
    f=self.ready();f.serial.before_parts=lambda:f.printer.reactor.register_async_callback(lambda t:f.printer.emit(event))
    with self.assertRaises(CommandError):f.snapshot()
    self.assert_clean(f);self.assertIsNone(f.status()['snapshot'])
    f.serial.before_parts=None;f.observer_connect();f.snapshot();self.assert_clean(f)
 def test_concurrent_query_and_snapshot_wait_for_the_first_snapshot(self):
  f=self.ready();other=threading.Thread(target=lambda:(f.query(),f.snapshot()))
  def concurrent():
   f.serial.before_parts=None;before=len(f.serial.commands)
   other.start();other.join(0.05);self.assertEqual(len(f.serial.commands),before)
  f.serial.before_parts=concurrent;f.snapshot();first=f.serial.parts[-1]['query'];other.join(5)
  self.assertEqual([n for n,_ in f.serial.commands[-12:]],['km_snapshot_part']*5+['km_transport_query']*2+['km_snapshot_part']*5)
  self.assertGreater(f.status()['snapshot']['query'],first);self.assert_clean(f)
 def test_delayed_old_callbacks_cannot_replace_new_snapshot(self):
  f=self.ready();f.snapshot();old=list(f.serial.saved_callbacks);f.serial.saved_callbacks=[]
  f.printer.emit('klippy:disconnect');f.serial.instance^=0x8000000000000000;f.observer_connect();f.snapshot();status=f.status()
  for cb,params in old:cb(params)
  f.printer.reactor.drain();self.assertEqual(f.status(),status);self.assert_clean(f)
 def test_malformed_matching_part_completes_with_error(self):
  f=self.ready();f.serial.direct=True;f.serial.transform=lambda n,v:{**v,'flags':True} if n==0 else v
  with self.assertRaises(CommandError):f.snapshot()
  self.assertIsNone(f.status()['snapshot']);self.assert_clean(f)
 def test_explicit_transport_observation_clears_snapshot(self):
  f=self.ready();f.snapshot();self.assertIsNotNone(f.status()['snapshot']);f.query()
  self.assertIsNone(f.status()['snapshot']);self.assert_clean(f)
 def test_unrelated_rejection_is_ignored_before_semantic_decode(self):
  for command,query in ((1,None),(8,99)):
   with self.subTest(command=command,query=query):
    f=self.ready()
    def unrelated():
     active=f.serial.parts[-1]['query']
     f.serial.emit('km_command_result',dict(instance_hi=0,instance_lo=0,query=active if query is None else query,command=command,result=255))
    f.serial.before_parts=unrelated;f.snapshot();self.assertIsNotNone(f.status()['snapshot']);self.assert_clean(f)
 def test_callback_queued_before_send_error_is_closed_on_unwind(self):
  f=self.ready();original=f.serial.raw_send_wait_ack;f.serial.fail_part=2
  def send_with_early_callback(data,*args):
   result=original(data,*args)
   if f.serial.parts and f.serial.parts[-1]['part']==0:
    q=f.serial.parts[-1]['query']
    f.serial.emit('km_command_result',dict(instance_hi=0,instance_lo=0,query=q,command=8,result=2))
   return result
  f.serial.raw_send_wait_ack=send_with_early_callback
  with self.assertRaises(CommandError):f.snapshot()
  self.assert_clean(f);completion=f.printer.reactor.completions[-1];self.assertFalse(completion.test())
  f.printer.reactor.drain();self.assertFalse(completion.test(),'closed transaction was completed by its queued old callback')
 def test_deadline_late_parts_never_publish(self):
  f=self.ready();original=f.printer.reactor.register_async_callback
  f.printer.reactor.register_async_callback=lambda cb,waketime=None:original(cb,f.printer.reactor.now+5.01)
  with self.assertRaises(CommandError):f.snapshot()
  self.assertIsNone(f.status()['snapshot']);self.assert_clean(f)
  f.printer.reactor.drain();self.assertIsNone(f.status()['snapshot']);self.assert_clean(f)
 def test_decoder_assembly_happens_on_reactor(self):
  f=self.ready();original=extra.SnapshotAssembler
  class CheckedAssembler(original):
   def add(self,name,params):
    assert f.printer.reactor.phase=='reactor','Decoder ran on serial thread'
    return super().add(name,params)
  with patch.object(extra,'SnapshotAssembler',CheckedAssembler):f.snapshot()
  self.assert_clean(f)
 def test_reconnect_during_snapshot_waits_for_unwind(self):
  f=self.ready();other=threading.Thread(target=f.observer_connect)
  def reconnect(t):
   f.printer.emit('klippy:disconnect');other.start()
  f.serial.before_parts=lambda:f.printer.reactor.register_async_callback(reconnect)
  with self.assertRaisesRegex(CommandError,'Stale snapshot response'):f.snapshot()
  f.serial.before_parts=None;other.join(5);self.assertTrue(f.status()['connected'])
  self.assert_clean(f);self.assertIsNone(f.status()['snapshot'])
  f.snapshot();self.assertIsNotNone(f.status()['snapshot']);self.assert_clean(f)
 def test_matching_malformed_admin_reply_remains_strict(self):
  f=self.ready();f.serial.response_error=255
  with self.assertRaises(CommandError):f.snapshot()
  self.assertIsNone(f.status()['snapshot']);self.assert_clean(f)
 def test_boolean_matching_fields_are_ignored(self):
  f=self.ready();f.serial.direct=True
  def foreign():
   f.serial.emit('km_command_result',dict(instance_hi=0,instance_lo=0,command=8,query=True,result=2))
  f.serial.before_parts=foreign;f.snapshot();self.assertIsNotNone(f.status()['snapshot']);self.assert_clean(f)
suite=unittest.TestSuite([unittest.defaultTestLoader.loadTestsFromTestCase(HostChecks),unittest.defaultTestLoader.loadTestsFromTestCase(SnapshotChecks),unittest.defaultTestLoader.loadTestsFromTestCase(DiagnosticChecks)])
with (a.results/'tests.log').open('w') as log:result=unittest.TextTestRunner(stream=log,verbosity=2).run(suite)
report={'status':'passed' if result.wasSuccessful() else 'failed','tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'hardware_access':False,
 'upstream_hashes':accepted,
 'package_sha256':{
  'host/__init__.py':hashlib.sha256((a.package/'host/__init__.py').read_bytes()).hexdigest(),
  'host/protocol.py':hashlib.sha256((a.package/'host/protocol.py').read_bytes()).hexdigest(),
 },
 'dictionary_sha256':hashlib.sha256(a.dictionary.read_bytes()).hexdigest(),
 'scope':'%d methods (20 observer + 21 snapshot + %d diagnostic startup) including 120 part-order permutations; actual public MCU wrappers (CommandQueryWrapper, CommandWrapper, AsyncResponseWrapper), MCUConfigHelper first connection, upstream Printer._connect error classification, SerialRetryCommand and MessageParser; modeled serial endpoint/reactor; diagnostic startup opt-in; no physical communication or boot entropy proof'%(result.testsRun,result.testsRun-41),
 'limitations':[
  'Upstream Printer._connect and _set_state run unchanged with no-op config reading and recorded events. Bad firmware constants give the upstream configuration error state. A failed first observation leaves the MMU unavailable while the printer reaches its ready state.',
  'Actual upstream public lookup, command encoders, decoder, retry, and first-connection MCUConfigHelper run against a modeled serial endpoint and reactor.',
  'Normal first connection follows config ordering. A subsequent observer connect does not rerun a finalized MCUConfigHelper. Matching-CRC init ordering uses a fresh Fixture whose MCU is already configured with the CRC predicted from a separate first connection.',
  'A changed dictionary is synthetic. No serial port, service, hardware, sleep, or real timer.',
  'No proof of entropy, source linking, OpenAMS lifecycle, motion readiness, or firmware latch idempotence. The MCU owns km_diagnostic_start; this host only registers the init command.',
  'Host-epoch checks use a synthetic dictionary with KLIPPER_MMU_BOOT_IDENTITY_HOST=1 and km_diagnostic_start_host, and a modeled one-binding latch. The epoch is Klipper CSPRNG output sent through the real CommandWrapper after MCUConfigHelper get_config and a registered post-init marker; it is a lifetime discriminator, not MCU entropy or authentication. Native firmware latch behavior is covered by tests/klipper_diagnostic_startup.',
  'Without diagnostic_startup the observer may send only km_transport_query. With the option the first connection also sends one km_diagnostic_start init and each observation sends km_diagnostic_query through the real CommandQueryWrapper. It never sends heartbeat, motion, acknowledgement, or session commands. Motion stays false. Actuation must be the integer 0 or 1 and query wrappers stay connection-scoped.',
  'Manual snapshots may also send km_snapshot_part and nothing else. Snapshot preflight calls the same _observe as query. There is no background polling. Partial registration is cleaned up. One snapshot wait is five seconds. Full-width IDs, high-water, and exhaustion are checked.',
  'Background callbacks are dispatched onto the modeled reactor. A stale callback does not complete a snapshot after error. Matching command results are applied and unrelated results are ignored. Duplicate and conflicting parts and generation recovery are checked.',
  'Diagnostic wire checks use MessageParser encode/decode of signed km_diagnostic_state. Boolean, missing, and non-integer decoder cases call _decode_startup directly because the wire formats normalize those inputs to integers. An extra metadata field on an otherwise valid startup response is accepted.',
  'If raw_send_wait_ack raises, pinned SerialRetryCommand keeps the km_diagnostic_state response callback. The host still clears the in-flight query guard and invalidates the observation and snapshot. Timeout unregisters that callback before raising. The next explicit query recovers through the real wrapper, clears the retained callback, sends no extra km_diagnostic_start, and uses a fresh increasing challenge.',
  'Modeled reactor, serial, and thread-phase checks are not concurrent hardware tests. Boot freshness and the full OpenAMS lifecycle remain out of scope.',
 ]}
(a.results/'results.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2));raise SystemExit(not result.wasSuccessful())
