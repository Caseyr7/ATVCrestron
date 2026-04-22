
import sys
import os
import asyncio
import json
import threading
import types
import ctypes
import ctypes.util

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_memfd_handles = []

def fix_abi_suffixes(deps_dir):
    '''Rename gnueabihf .so files to gnueabi to match Crestron Python.'''
    import importlib.machinery
    suffixes = importlib.machinery.EXTENSION_SUFFIXES
    if not any('gnueabi.so' in s for s in suffixes):
        return
    for root, dirs, files in os.walk(deps_dir):
        for fname in files:
            if 'gnueabihf.so' in fname:
                old_path = os.path.join(root, fname)
                new_path = os.path.join(root, fname.replace('gnueabihf.so', 'gnueabi.so'))
                if os.path.exists(new_path):
                    os.unlink(new_path)
                os.rename(old_path, new_path)

def patch_so_files(deps_dir):
    libc = ctypes.CDLL(ctypes.util.find_library('c'))
    libc.memfd_create.argtypes = [ctypes.c_char_p, ctypes.c_uint]
    libc.memfd_create.restype = ctypes.c_int
    for root, dirs, files in os.walk(deps_dir):
        for fname in files:
            if '.so' not in fname:
                continue
            so_path = os.path.join(root, fname)
            if os.path.islink(so_path):
                continue
            with open(so_path, 'rb') as f:
                data = f.read()
            fd = libc.memfd_create(fname.encode(), 0)
            if fd < 0:
                continue
            os.write(fd, data)
            os.lseek(fd, 0, os.SEEK_SET)
            _memfd_handles.append(fd)
            backup = so_path + '.orig'
            if not os.path.exists(backup):
                os.rename(so_path, backup)
            else:
                os.unlink(so_path)
            os.symlink('/proc/self/fd/' + str(fd), so_path)

def restore_so_files(deps_dir):
    for root, dirs, files in os.walk(deps_dir):
        for fname in files:
            if not fname.endswith('.orig'):
                continue
            orig = os.path.join(root, fname)
            target = orig[:-5]
            if os.path.islink(target):
                os.unlink(target)
            if os.path.exists(target):
                os.unlink(target)
            os.rename(orig, target)

# Load deps path from installer, apply ABI fix + memfd patches
_deps_file = os.path.join(SCRIPT_DIR, '.deps_path')
if os.path.exists(_deps_file):
    with open(_deps_file) as f:
        _deps_dir = f.read().strip()
    if os.path.isdir(_deps_dir):
        restore_so_files(_deps_dir)
        fix_abi_suffixes(_deps_dir)
        patch_so_files(_deps_dir)
        sys.path.insert(0, _deps_dir)

# Stub out miniaudio (all attributes pyatv uses)
_mini = types.ModuleType('miniaudio')
_mini.SampleFormat = type('SampleFormat', (), {'SIGNED16': 0, 'FLOAT32': 1})
_mini.PlaybackDevice = type('PlaybackDevice', (), {})
_mini.DecodeError = type('DecodeError', (Exception,), {})
_mini.StreamableSource = type('StreamableSource', (), {
    'read': lambda self, n: b'',
    'seek': lambda self, o, w: False,
})
_mini.SeekOrigin = type('SeekOrigin', (), {'SET': 0, 'CURRENT': 1})
_mini.WavFileReadStream = type('WavFileReadStream', (), {'__init__': lambda self, *a, **kw: None})
_mini.DecodedSoundFile = type('DecodedSoundFile', (), {})
_mini.get_file_info = lambda *a, **kw: None
_mini.stream_any = lambda *a, **kw: iter([])
_mini.decode_file = lambda *a, **kw: None
_mini.__version__ = '0.0.0'
sys.modules['miniaudio'] = _mini

import struct
import time as _time
try:
    import urllib.request
    import urllib.parse
except ImportError:
    urllib = None

try:
    import pyatv
    from pyatv.interface import PushListener
    from pyatv.const import Protocol, ShuffleState, RepeatState, PowerState
    try:
        from pyatv.const import FeatureName, FeatureState as FState
        _has_features = True
    except ImportError:
        _has_features = False
    try:
        from pyatv.interface import PowerListener as IPowerListener
        _has_power_listener = True
    except ImportError:
        _has_power_listener = False
except ImportError as e:
    def crestron_main(mod):
        mod.set('ERROR:pyatv not installed: ' + str(e))
    raise SystemExit(1)

CREDS_FILE = os.path.join(SCRIPT_DIR, 'appletv_credentials.json')
_DIAG_FILE = os.path.join(SCRIPT_DIR, 'bridge_diag.log')
ICON_DIR = os.path.join(SCRIPT_DIR, 'app_icons')
g_atv = None
g_pairing = None
g_pair_protocol = None
g_pair_ip = None
g_mod = None
g_loop = None
g_cmd_queue = None
g_debug_level = 0  # 0=none 1=SPLUS 2=SS 3=PY 4=all
g_rc_logged = False
g_app_list = []
g_touch_state = {'down_time': 0, 'tap_count': 0, 'pending': None}
g_init_ip = None  # last IP passed to INIT, used for auto-reconnect
g_reconnect_count = 0
ART_FILE = os.path.join(SCRIPT_DIR, 'app_icons', 'now_playing_art.png')
g_last_art_title = ''  # title of last artwork fetched

def _diag(msg):
    '''Write diagnostic message to file for debugging SendData issues.'''
    try:
        import datetime
        ts = datetime.datetime.now().strftime('%H:%M:%S.%f')
        with open(_DIAG_FILE, 'a') as f:
            f.write(ts + ' ' + str(msg) + '\n')
    except Exception:
        pass

def send(msg):
    if g_mod:
        _diag('SEND>' + msg[:800])
        g_mod.set(msg)

def debug(msg):
    '''Send debug message if Python debug is enabled (level 3 or 4).'''
    if g_debug_level >= 3:
        send('DEBUG:[PY] ' + msg)

class Listener(PushListener):
    def playstatus_update(self, updater, ps):
        global g_last_art_title
        try:
            _diag('PUSH_UPDATE:state=' + str(ps.device_state) + ' title=' + str(ps.title))
            info = {
                'state':      str(ps.device_state),
                'title':      ps.title or '',
                'artist':     ps.artist or '',
                'album':      ps.album or '',
                'position':   ps.position or 0,
                'total_time': ps.total_time or 0,
                'shuffle':    str(ps.shuffle),
                'repeat':     str(ps.repeat),
            }
            send('NOW_PLAYING:' + json.dumps(info))
            # Auto-fetch artwork when title changes
            _t = ps.title or ''
            if _t and _t != g_last_art_title and g_atv and g_loop:
                g_last_art_title = _t
                asyncio.run_coroutine_threadsafe(do_artwork(), g_loop)
            # Fetch current app on state changes
            if g_atv and g_loop:
                async def _send_app():
                    try:
                        app = await g_atv.metadata.app
                        if app:
                            send('CURRENT_APP:' + str(app.identifier) + ':' + str(app.name))
                    except Exception:
                        pass
                asyncio.run_coroutine_threadsafe(_send_app(), g_loop)
        except Exception as e:
            _diag('PUSH_UPDATE_ERR:' + str(e))
            send('ERROR:push:' + str(e))

    def playstatus_error(self, updater, exception):
        _diag('PUSH_ERROR:' + str(exception))
        send('ERROR:push_error:' + str(exception))

if _has_power_listener:
    class MyPowerListener(IPowerListener):
        def powerstate_update(self, old_state, new_state):
            _diag('POWER>' + str(old_state) + '->' + str(new_state))
            ns = str(new_state)
            if 'On' in ns:
                send('POWER_STATE:On')
            elif 'Off' in ns:
                send('POWER_STATE:Off')
            else:
                send('POWER_STATE:Unknown')

def load_creds():
    try:
        if os.path.exists(CREDS_FILE):
            with open(CREDS_FILE, 'r') as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def save_cred(ip, proto_name, cred_str):
    try:
        data = load_creds()
        if ip not in data:
            data[ip] = {}
        data[ip][proto_name] = cred_str
        os.makedirs(os.path.dirname(CREDS_FILE), exist_ok=True)
        with open(CREDS_FILE, 'w') as f:
            json.dump(data, f)
    except Exception as e:
        send('ERROR:save_cred:' + str(e))

async def do_connect(ip):
    global g_atv, g_rc_logged
    try:
        g_rc_logged = False
        if g_atv:
            try:
                g_atv.push_updater.stop()
                g_atv.close()
            except Exception:
                pass
            g_atv = None

        debug('Scanning for ' + ip + '...')
        creds = load_creds()
        config = None

        # Try scan first (works when device is awake/advertising)
        atvs = await pyatv.scan(g_loop, hosts=[ip], timeout=5)
        if atvs:
            config = atvs[0]
            debug('Found via scan: ' + config.name)
        elif ip in creds:
            # Device not advertising (asleep?) but we have saved credentials
            # Build a manual config so pyatv.connect can wake it via MRP/Companion
            debug('Scan failed, building manual config from saved credentials...')
            try:
                from pyatv.conf import AppleTV, ManualService
                from pyatv.const import Protocol as P
                import ipaddress
                # Extract device identifier from credential string
                # Credential format: seed:auth_key:device_id_hex:output_key
                dev_id = None
                for cred_str in creds[ip].values():
                    parts = cred_str.split(':')
                    if len(parts) >= 3:
                        try:
                            dev_id = bytes.fromhex(parts[2]).decode('ascii')
                            break
                        except Exception:
                            pass
                if dev_id:
                    debug('Extracted device ID: ' + dev_id)
                else:
                    debug('Could not extract device ID from credentials, using placeholder')
                    dev_id = 'manual-' + ip.replace('.', '-')
                config = AppleTV(ipaddress.IPv4Address(ip), 'Apple TV', deep_sleep=True)
                # Add service entries for each saved protocol with credentials
                _proto_ports = {'Companion': 49152, 'AirPlay': 7000, 'MRP': 49152, 'RAOP': 7000}
                for proto_name, cred_str in creds[ip].items():
                    try:
                        proto = P[proto_name]
                        port = _proto_ports.get(proto_name, 49152)
                        config.add_service(ManualService(dev_id, proto, port, {}, credentials=cred_str))
                        debug('Added manual service: ' + proto_name + ' port=' + str(port))
                    except Exception as se:
                        _diag('Manual svc error(' + proto_name + '): ' + str(se))
            except Exception as me:
                _diag('Manual config error: ' + str(me))
                send('ERROR:No Apple TV found at ' + ip + ' (manual config failed: ' + str(me) + ')')
                return
        else:
            send('ERROR:No Apple TV found at ' + ip + ' (no saved credentials)')
            return

        # Log available services/protocols
        svc_info = []
        for svc in config.services:
            svc_info.append(str(svc.protocol) + '(port=' + str(svc.port) + ')')
        _diag('Services for ' + config.name + ': ' + ', '.join(svc_info))
        send('DEBUG:[PY] Services: ' + ', '.join(svc_info))
        debug('Connecting to ' + config.name + '...')
        if ip in creds:
            for proto_name, cred_str in creds[ip].items():
                try:
                    config.set_credentials(Protocol[proto_name], cred_str)
                except Exception:
                    pass

        g_atv = await pyatv.connect(config, loop=g_loop)

        # Log which interfaces/features are available
        ifaces = []
        for attr in ['metadata', 'push_updater', 'remote_control', 'power', 'apps', 'audio']:
            obj = getattr(g_atv, attr, None)
            if obj:
                proto = getattr(obj, 'main_protocol', None)
                ifaces.append(attr + '(' + str(proto) + ')')
            else:
                ifaces.append(attr + '(NONE)')
        _diag('Interfaces: ' + ', '.join(ifaces))

        g_atv.push_updater.listener = Listener()
        g_atv.push_updater.start(initial_delay=0)
        _diag('Push updater started')

        # Register power listener and send initial state
        pwr = getattr(g_atv, 'power', None)
        if pwr:
            try:
                if _has_power_listener:
                    pwr.listener = MyPowerListener()
                ps = pwr.power_state
                ns = str(ps)
                if 'On' in ns:
                    send('POWER_STATE:On')
                elif 'Off' in ns:
                    send('POWER_STATE:Off')
                else:
                    send('POWER_STATE:Unknown')
            except Exception as pe:
                _diag('Power init error: ' + str(pe))

        # Test metadata and warn if not available via MRP
        meta_proto = getattr(g_atv.metadata, 'main_protocol', None)
        try:
            ps = await g_atv.metadata.playing()
            _diag('Metadata(' + str(meta_proto) + '): state=' + str(ps.device_state) + ' title=' + str(ps.title))
        except Exception as me:
            _diag('Metadata ERROR: ' + str(me))

        if meta_proto and 'RAOP' in str(meta_proto):
            send('DEBUG:[PY] WARNING: Pairing incomplete - Make sure Require Password under AirPlay is not enabled for MetaData')

        # Send device info
        try:
            di = config.device_info if hasattr(config, 'device_info') else None
            if not di:
                di = getattr(g_atv, 'device_info', None)
            if di:
                info = {}
                for attr in ['model', 'model_str', 'operating_system', 'version', 'mac']:
                    v = getattr(di, attr, None)
                    if v is not None:
                        info[attr] = str(v)
                send('DEVICE_INFO:' + json.dumps(info))
        except Exception as die:
            _diag('DeviceInfo err: ' + str(die))

        # Send current app
        try:
            app = await g_atv.metadata.app
            if app:
                send('CURRENT_APP:' + str(app.identifier) + ':' + str(app.name))
        except Exception:
            pass

        # Send initial volume
        try:
            audio = getattr(g_atv, 'audio', None)
            if audio:
                vol = audio.volume
                if vol is not None:
                    send('VOLUME_LEVEL:' + str(int(vol)))
        except Exception:
            pass

        # Send feature availability
        if _has_features:
            try:
                ft = g_atv.features
                avail = []
                for fn in [FeatureName.PlayPause, FeatureName.Play, FeatureName.Pause,
                           FeatureName.Stop, FeatureName.Next, FeatureName.Previous,
                           FeatureName.VolumeUp, FeatureName.VolumeDown, FeatureName.SetVolume,
                           FeatureName.SetPosition, FeatureName.SetShuffle, FeatureName.SetRepeat,
                           FeatureName.TurnOn, FeatureName.TurnOff,
                           FeatureName.AppList, FeatureName.LaunchApp,
                           FeatureName.Artwork, FeatureName.PushUpdates]:
                    try:
                        if ft.in_state(FState.Available, fn):
                            avail.append(fn.name)
                    except Exception:
                        pass
                send('FEATURES:' + ','.join(avail))
            except Exception as fe:
                _diag('Features err: ' + str(fe))

        send('CONNECTED:' + config.name)
    except Exception as e:
        send('ERROR:connect:' + str(type(e).__name__) + ':' + str(e))

async def do_pair_start(ip, force_proto=None):
    global g_pairing, g_pair_protocol, g_pair_ip
    try:
        g_pair_ip = ip
        debug('Pair scanning for ' + ip + '...')
        atvs = await pyatv.scan(g_loop, hosts=[ip], timeout=5)
        if not atvs:
            send('PAIR_ERROR:No Apple TV at ' + ip)
            return
        config = atvs[0]
        avail_protos = [svc.protocol for svc in config.services]
        _diag('Pair protocols available: ' + str(avail_protos))

        if force_proto:
            pair_proto = force_proto
        else:
            # Determine best protocol for pairing: Companion > MRP > AirPlay
            pair_proto = None
            for try_proto in [Protocol.Companion, Protocol.MRP, Protocol.AirPlay]:
                if try_proto in avail_protos:
                    pair_proto = try_proto
                    break
        if not pair_proto:
            send('PAIR_ERROR:No supported pairing protocol found')
            return
        g_pair_protocol = pair_proto
        debug('Starting ' + str(pair_proto) + ' pairing...')
        send('DEBUG:[PY] Pairing via ' + str(pair_proto))
        g_pairing = await pyatv.pair(config, pair_proto, loop=g_loop)
        await g_pairing.begin()
        if pair_proto == Protocol.AirPlay:
            # AirPlay pairing may not require a PIN (device-dependent)
            if g_pairing.device_provides_pin:
                send('PAIR_WAITING_PIN')
            else:
                # Auto-finish if no PIN needed
                g_pairing.pin(0)
                await g_pairing.finish()
                if g_pairing.has_paired:
                    svc = g_pairing.service
                    cred_ip = g_pair_ip or str(svc.address)
                    save_cred(cred_ip, 'AirPlay', svc.credentials)
                    send('PAIR_OK:AirPlay')
                    debug('AirPlay paired (no PIN) for ' + cred_ip)
                    g_pairing = None
                    g_pair_ip = None
                    return
                else:
                    send('PAIR_WAITING_PIN')
        else:
            send('PAIR_WAITING_PIN')
    except Exception as e:
        send('PAIR_ERROR:' + str(type(e).__name__) + ':' + str(e))

async def do_pair_pin(pin):
    global g_pairing, g_pair_protocol, g_pair_ip
    try:
        if not g_pairing:
            send('PAIR_ERROR:No active pairing session')
            return
        g_pairing.pin(int(pin))
        await g_pairing.finish()
        if g_pairing.has_paired:
            svc = g_pairing.service
            proto_name = g_pair_protocol.name if g_pair_protocol else 'MRP'
            # Use g_pair_ip (from PAIR_START) so INIT:ip lookup matches
            cred_ip = g_pair_ip or str(svc.address)
            save_cred(cred_ip, proto_name, svc.credentials)
            send('PAIR_OK:' + proto_name)
            debug('Paired via ' + proto_name + ' for ' + cred_ip + ', credentials saved')
            reconnect_ip = cred_ip
            paired_proto = g_pair_protocol
            g_pairing = None
            g_pair_ip = None
            # If we just paired Companion, also pair AirPlay for metadata
            if paired_proto == Protocol.Companion:
                debug('Companion paired, now auto-pairing AirPlay for metadata...')
                await do_pair_start(reconnect_ip, force_proto=Protocol.AirPlay)
                # If AirPlay auto-completed (no PIN), do_pair_start handles it
                # If it needs a PIN, PAIR_WAITING_PIN was sent and we wait
                if g_pairing is None:
                    # AirPlay pairing completed, reconnect with all creds
                    debug('Auto-reconnecting to ' + reconnect_ip + ' with all credentials...')
                    await do_connect(reconnect_ip)
                # else: waiting for AirPlay PIN, user will send PAIR_PIN
                return
            # Auto-reconnect with new credentials
            debug('Auto-reconnecting to ' + reconnect_ip + ' with new credentials...')
            await do_connect(reconnect_ip)
            return
        else:
            send('PAIR_ERROR:PIN rejected')
        g_pairing = None
        g_pair_ip = None
    except Exception as e:
        send('PAIR_ERROR:' + str(type(e).__name__) + ':' + str(e))
        g_pairing = None
        g_pair_ip = None

async def do_artwork():
    try:
        if not g_atv:
            send('ERROR:artwork:Not connected')
            return
        artwork = await g_atv.metadata.artwork()
        if artwork and artwork.bytes:
            os.makedirs(os.path.dirname(ART_FILE), exist_ok=True)
            with open(ART_FILE, 'wb') as f:
                f.write(artwork.bytes)
            send('NOW_PLAYING_ART:' + ART_FILE)
        else:
            send('NOW_PLAYING_ART:')
    except Exception as e:
        _diag('Artwork err: ' + str(e))
        send('NOW_PLAYING_ART:')

async def do_get_volume():
    try:
        audio = getattr(g_atv, 'audio', None)
        if audio:
            vol = audio.volume
            if vol is not None:
                send('VOLUME_LEVEL:' + str(int(vol)))
            else:
                send('VOLUME_LEVEL:0')
        else:
            send('ERROR:volume:audio interface not available')
    except Exception as e:
        send('ERROR:volume:' + str(e))

async def do_set_volume(val):
    try:
        audio = getattr(g_atv, 'audio', None)
        if audio:
            await audio.set_volume(float(val))
            send('ACK:SET_VOLUME:' + str(val))
            send('VOLUME_LEVEL:' + str(int(float(val))))
        else:
            send('ERROR:set_volume:audio interface not available')
    except Exception as e:
        send('ERROR:set_volume:' + str(e))

async def do_get_current_app():
    try:
        app = await g_atv.metadata.app
        if app:
            send('CURRENT_APP:' + str(app.identifier) + ':' + str(app.name))
        else:
            send('CURRENT_APP::')
    except Exception as e:
        send('ERROR:current_app:' + str(e))

async def do_discover():
    try:
        debug('Scanning network for Apple TVs...')
        atvs = await pyatv.scan(g_loop, timeout=5)
        results = []
        for atv in atvs:
            name = atv.name or ''
            model_str = ''
            try:
                model_str = str(atv.device_info.model) if atv.device_info else ''
            except Exception:
                pass
            addr = str(atv.address) if atv.address else ''
            results.append({'name': name, 'model': model_str, 'address': addr})
        debug('Scan found ' + str(len(results)) + ' device(s)')
        send('SCAN_RESULTS:' + json.dumps(results))
    except Exception as e:
        send('ERROR:discover:' + str(type(e).__name__) + ':' + str(e))

async def do_list_apps_full():
    global g_app_list
    try:
        if not g_atv:
            send('ERROR:list_apps:Not connected')
            return
        apps = await g_atv.apps.app_list()
        g_app_list = [{'id': a.identifier, 'name': a.name} for a in apps]
        count = min(len(g_app_list), 100)
        send('APP_LIST_BEGIN:' + str(count))
        for i in range(count):
            app = g_app_list[i]
            send('APP_ITEM:' + str(i + 1) + ':' + app['id'] + ':' + app['name'])
        send('APP_LIST_END')
        # Also send combined JSON for AppList$ serial output
        send('APP_LIST:' + json.dumps(g_app_list))
        debug('Found ' + str(len(g_app_list)) + ' apps')
        # Download icons in background
        asyncio.ensure_future(_download_icons(), loop=g_loop)
    except Exception as e:
        send('ERROR:list_apps:' + str(e))

async def _download_icons():
    if not urllib:
        _diag('ICON:urllib not available, skipping icon download')
        return
    try:
        os.makedirs(ICON_DIR, exist_ok=True)
    except Exception:
        pass
    for i, app in enumerate(g_app_list[:100]):
        bid = app.get('id', '')
        if not bid:
            continue
        icon_path = os.path.join(ICON_DIR, bid.replace('.', '_') + '.png')
        if not os.path.exists(icon_path):
            try:
                url = 'https://itunes.apple.com/lookup?bundleId=' + urllib.parse.quote(bid)
                req = urllib.request.urlopen(url, timeout=10)
                data = json.loads(req.read().decode())
                if data.get('resultCount', 0) > 0:
                    art_url = data['results'][0].get('artworkUrl100', '')
                    if art_url:
                        urllib.request.urlretrieve(art_url, icon_path)
                        _diag('ICON:OK:' + bid)
            except Exception as e:
                _diag('ICON:FAIL:' + bid + ':' + str(e))
                continue
        if os.path.exists(icon_path):
            send('APP_ICON:' + str(i + 1) + ':' + icon_path)

async def do_select_app(index):
    try:
        idx = int(index) - 1
        if idx < 0 or idx >= len(g_app_list):
            send('ERROR:select_app:Index ' + str(idx + 1) + ' out of range (have ' + str(len(g_app_list)) + ')')
            return
        app = g_app_list[idx]
        bid = app['id']
        debug('Launching app: ' + app['name'] + ' (' + bid + ')')
        await g_atv.apps.launch_app(bid)
        send('ACK:SELECT_APP:' + str(idx + 1) + ':' + app['name'])
    except Exception as e:
        send('ERROR:select_app:' + str(e))

async def do_keyboard_text(text):
    try:
        # MrpRemoteControl has a .protocol attribute that is MrpProtocol
        # Iterate through all RC instances and find the MRP one by module path
        mrp_proto = None
        rc = getattr(g_atv, 'remote_control', None)
        if rc and hasattr(rc, 'instances'):
            for inst in rc.instances:
                mod = type(inst).__module__
                _diag('KB:inst module=' + mod + ' class=' + type(inst).__name__)
                if 'mrp' in mod.lower():
                    mrp_proto = getattr(inst, 'protocol', None)
                    if mrp_proto:
                        _diag('KB:found MRP protocol via ' + mod)
                        break

        if not mrp_proto:
            send('ERROR:keyboard:MRP instance not found (instances=' +
                 str([type(i).__name__ for i in (rc.instances if rc else [])]) + ')')
            return

        from pyatv.protocols.mrp import protobuf as pb
        from pyatv.protocols.mrp.protobuf import TextInputMessage_pb2
        import time as _t

        msg = pb.ProtocolMessage()
        msg.type = pb.ProtocolMessage.TEXT_INPUT_MESSAGE  # = 25
        inner = msg.Extensions[TextInputMessage_pb2.textInputMessage]
        inner.timestamp = _t.time()
        inner.text = text
        inner.actionType = 1  # Insert

        await mrp_proto.send(msg)
        send('ACK:KEYBOARD_TEXT:' + text[:40])
    except Exception as e:
        _diag('KEYBOARD err: ' + str(type(e).__name__) + ':' + str(e))
        send('ERROR:keyboard:' + str(type(e).__name__) + ':' + str(e))

async def do_touch_action(x, y, action):
    try:
        if action == 'tap':
            await g_atv.remote_control.select()
            send('ACK:TOUCH_TAP')
            return
        elif action == 'double_tap':
            await g_atv.remote_control.select()
            await asyncio.sleep(0.1)
            await g_atv.remote_control.select()
            send('ACK:TOUCH_DOUBLE_TAP')
            return
        elif action == 'hold':
            await g_atv.remote_control.home_hold()
            send('ACK:TOUCH_HOLD')
            return
        # If we get here, try raw MRP touch events
        send('ERROR:touch:Unknown action ' + action)
    except Exception as e:
        send('ERROR:touch:' + str(e))

async def handle_touch_down(x, y):
    g_touch_state['down_time'] = _time.time()
    g_touch_state['x'] = x
    g_touch_state['y'] = y
    if g_touch_state.get('pending'):
        g_touch_state['pending'].cancel()
        g_touch_state['pending'] = None

async def handle_touch_up(x, y):
    duration = _time.time() - g_touch_state.get('down_time', 0)
    if duration >= 1.0:
        await do_touch_action(x, y, 'hold')
        g_touch_state['tap_count'] = 0
    else:
        g_touch_state['tap_count'] = g_touch_state.get('tap_count', 0) + 1
        if g_touch_state['tap_count'] >= 2:
            g_touch_state['tap_count'] = 0
            if g_touch_state.get('pending'):
                g_touch_state['pending'].cancel()
                g_touch_state['pending'] = None
            await do_touch_action(x, y, 'double_tap')
        else:
            async def _single_tap():
                await asyncio.sleep(0.35)
                if g_touch_state.get('tap_count', 0) == 1:
                    g_touch_state['tap_count'] = 0
                    await do_touch_action(x, y, 'tap')
            g_touch_state['pending'] = asyncio.ensure_future(_single_tap(), loop=g_loop)

async def do_self_test():
    results = []
    ok = 0
    fail = 0

    def r(name, status, detail=''):
        nonlocal ok, fail
        if status == 'OK':
            ok += 1
        else:
            fail += 1
        entry = 'TEST:' + name + ':' + status
        if detail:
            entry += ':' + detail
        results.append(entry)
        _diag(entry)

    # 1. Metadata
    try:
        ps = await g_atv.metadata.playing()
        r('metadata.playing', 'OK', 'state=' + str(ps.device_state) + ' title=' + str(ps.title)[:40])
    except Exception as e:
        r('metadata.playing', 'FAIL', str(e))

    # 2. Push updater
    try:
        pu = g_atv.push_updater
        r('push_updater', 'OK', 'proto=' + str(getattr(pu, 'main_protocol', '?')))
    except Exception as e:
        r('push_updater', 'FAIL', str(e))

    # 3. Remote control - check each method exists (no execution)
    rc = g_atv.remote_control
    rc_cmds = {
        'play': 'play', 'pause': 'pause', 'stop': 'stop',
        'next': 'next', 'previous': 'previous',
        'up': 'up', 'down': 'down', 'left': 'left', 'right': 'right',
        'select': 'select', 'menu': 'menu',
        'home': 'home', 'home_hold': 'home_hold',
        'volume_up': 'volume_up', 'volume_down': 'volume_down',
        'channel_up': 'channel_up', 'channel_down': 'channel_down',
        'skip_forward': 'skip_forward', 'skip_backward': 'skip_backward',
        'set_position': 'set_position', 'set_shuffle': 'set_shuffle',
        'set_repeat': 'set_repeat',
    }
    for name, method_name in rc_cmds.items():
        method = getattr(rc, method_name, None)
        if method and callable(method):
            r('rc.' + name, 'OK')
        else:
            r('rc.' + name, 'FAIL', 'method not found')

    # 4. Power interface
    pwr = getattr(g_atv, 'power', None)
    if pwr:
        for name in ['turn_on', 'turn_off']:
            method = getattr(pwr, name, None)
            if method and callable(method):
                r('power.' + name, 'OK')
            else:
                r('power.' + name, 'FAIL', 'method not found')
    else:
        r('power', 'FAIL', 'interface not available')

    # 5. Apps interface
    try:
        app_list = await g_atv.apps.app_list()
        r('apps.app_list', 'OK', str(len(app_list)) + ' apps')
    except Exception as e:
        r('apps.app_list', 'FAIL', str(e))

    # 6. Audio interface
    audio = getattr(g_atv, 'audio', None)
    if audio:
        r('audio', 'OK', 'proto=' + str(getattr(audio, 'main_protocol', '?')))
    else:
        r('audio', 'FAIL', 'interface not available')

    # 7. Live test - execute a safe command (play_pause to verify execution path)
    try:
        # Use select as a safe live test - just a brief press
        await rc.select()
        r('rc.select(live)', 'OK', 'executed')
    except Exception as e:
        r('rc.select(live)', 'FAIL', str(e))

    # Summary
    summary = 'SELF_TEST_DONE:' + str(ok) + ' passed, ' + str(fail) + ' failed'
    _diag(summary)
    send(summary)
    for entry in results:
        if 'FAIL' in entry:
            send('DEBUG:[PY] ' + entry)

async def handle_cmd(cmd):
    global g_debug_level, g_reconnect_count

    if cmd.startswith('SET_DEBUG:'):
        try:
            g_debug_level = int(cmd[10:].strip())
            debug('Debug level set to ' + str(g_debug_level))
        except Exception:
            pass
        return

    if cmd == 'DISCOVER':
        await do_discover()
        return

    if cmd.startswith('INIT:'):
        g_init_ip = cmd[5:].strip()
        g_reconnect_count = 0
        await do_connect(g_init_ip)
        return

    if cmd == 'STATUS':
        if g_atv:
            send('STATUS:CONNECTED')
        else:
            send('STATUS:DISCONNECTED')
            # Auto-reconnect: if we had a connection and lost it, try again
            if g_init_ip and g_reconnect_count < 3:
                g_reconnect_count = g_reconnect_count + 1
                debug('Auto-reconnect attempt ' + str(g_reconnect_count) + ' to ' + g_init_ip)
                await do_connect(g_init_ip)
        return

    if cmd.startswith('PAIR_START:'):
        await do_pair_start(cmd[11:].strip())
        return

    if cmd.startswith('PAIR_AIRPLAY:'):
        await do_pair_start(cmd[13:].strip(), force_proto=Protocol.AirPlay)
        return

    if cmd.startswith('PAIR_PIN:'):
        await do_pair_pin(cmd[9:].strip())
        return

    if cmd == 'NOW_PLAYING':
        if not g_atv:
            send('ERROR:Not connected')
            return
        try:
            ps = await g_atv.metadata.playing()
            info = {
                'state':      str(ps.device_state),
                'title':      ps.title or '',
                'artist':     ps.artist or '',
                'album':      ps.album or '',
                'position':   ps.position or 0,
                'total_time': ps.total_time or 0,
                'shuffle':    str(ps.shuffle),
                'repeat':     str(ps.repeat),
            }
            # Include current app info in now playing
            try:
                app = await g_atv.metadata.app
                if app:
                    info['app_id'] = str(app.identifier)
                    info['app_name'] = str(app.name)
            except Exception:
                pass
            send('NOW_PLAYING:' + json.dumps(info))
            # Reset reconnect counter on successful poll
            g_reconnect_count = 0
        except Exception as e:
            msg = str(e)
            if 'blocked' in msg:
                _diag('metadata blocked (pairing in progress)')
            else:
                send('ERROR:now_playing:' + msg)
        return

    if cmd == 'ARTWORK':
        await do_artwork()
        return

    if cmd == 'GET_VOLUME':
        if not g_atv:
            send('ERROR:Not connected')
            return
        await do_get_volume()
        return

    if cmd.startswith('SET_VOLUME:'):
        if not g_atv:
            send('ERROR:Not connected')
            return
        await do_set_volume(cmd[11:].strip())
        return

    if cmd == 'GET_APP':
        if not g_atv:
            send('ERROR:Not connected')
            return
        await do_get_current_app()
        return

    if cmd == 'SELF_TEST':
        if not g_atv:
            send('ERROR:Not connected')
            return
        await do_self_test()
        return

    if cmd == 'LIST_APPS_FULL':
        await do_list_apps_full()
        return

    if cmd.startswith('SELECT_APP:'):
        await do_select_app(cmd[11:].strip())
        return

    if cmd.startswith('KEYBOARD_TEXT:'):
        await do_keyboard_text(cmd[14:].strip())
        return

    if cmd.startswith('TOUCH_DOWN:'):
        parts = cmd[11:].split(',')
        if len(parts) == 2:
            await handle_touch_down(float(parts[0]), float(parts[1]))
        return

    if cmd.startswith('TOUCH_UP:'):
        parts = cmd[9:].split(',')
        if len(parts) == 2:
            await handle_touch_up(float(parts[0]), float(parts[1]))
        return

    if not g_atv:
        send('ERROR:Not connected')
        return

    try:
        global g_rc_logged
        rc = g_atv.remote_control
        # Log available methods once for diagnostics
        if not g_rc_logged:
            rc_methods = [m for m in dir(rc) if not m.startswith('_')]
            _diag('RC methods: ' + str(rc_methods))
            g_rc_logged = True

        # Power commands use the power interface, not remote_control
        if cmd == 'TURN_ON':
            pwr = getattr(g_atv, 'power', None)
            if pwr:
                await pwr.turn_on()
                send('ACK:TURN_ON')
                send('POWER_STATE:On')
            else:
                send('ERROR:TURN_ON:power interface not available')
            return
        if cmd == 'TURN_OFF':
            pwr = getattr(g_atv, 'power', None)
            if pwr:
                await pwr.turn_off()
                send('ACK:TURN_OFF')
                send('POWER_STATE:Off')
            else:
                send('ERROR:TURN_OFF:power interface not available')
            return

        # Map command names to remote_control method names
        # Use getattr to avoid crashing if a method is missing in this pyatv version
        _cmd_methods = {
            'PLAY': 'play', 'PAUSE': 'pause', 'STOP': 'stop',
            'NEXT': 'next', 'PREVIOUS': 'previous',
            'UP': 'up', 'DOWN': 'down', 'LEFT': 'left', 'RIGHT': 'right',
            'SELECT': 'select', 'MENU': 'menu',
            'HOME': 'home', 'HOME_HOLD': 'home_hold',
            'VOLUME_UP': 'volume_up', 'VOLUME_DOWN': 'volume_down',
            'CHANNEL_UP': 'channel_up', 'CHANNEL_DOWN': 'channel_down',
            'SKIP_FORWARD': 'skip_forward', 'SKIP_BACKWARD': 'skip_backward',
            'FAST_FORWARD': 'fast_forward', 'REWIND': 'rewind',
            'SCREENSAVER': 'screensaver',
            'PLAY_PAUSE': 'play_pause',
        }

        if cmd in _cmd_methods:
            method = getattr(rc, _cmd_methods[cmd], None)
            if method:
                await method()
                send('ACK:' + cmd)
            else:
                send('ERROR:' + cmd + ':not supported by this Apple TV')
            return
        elif cmd.startswith('LAUNCH_APP:'):
            app_id = cmd[11:].strip()
            await g_atv.apps.launch_app(app_id)
            send('ACK:LAUNCH_APP:' + app_id)
        elif cmd == 'LIST_APPS':
            apps = await g_atv.apps.app_list()
            result = [{'id': a.identifier, 'name': a.name} for a in apps]
            send('APP_LIST:' + json.dumps(result))
        elif cmd.startswith('SEEK:'):
            pos = int(cmd[5:].strip())
            await rc.set_position(pos)
            send('ACK:SEEK:' + str(pos))
        elif cmd.startswith('SET_SHUFFLE:'):
            val = cmd[12:].strip()
            state = ShuffleState.Songs if val == '1' else ShuffleState.Off
            await rc.set_shuffle(state)
            send('ACK:SET_SHUFFLE:' + val)
        elif cmd.startswith('SET_REPEAT:'):
            val = cmd[11:].strip()
            mapping = {'0': RepeatState.Off, '1': RepeatState.Track, '2': RepeatState.All}
            await rc.set_repeat(mapping.get(val, RepeatState.Off))
            send('ACK:SET_REPEAT:' + val)
        elif cmd == 'SHUFFLE_TOGGLE':
            ps = await g_atv.metadata.playing()
            if 'Off' in str(ps.shuffle):
                await rc.set_shuffle(ShuffleState.Songs)
            else:
                await rc.set_shuffle(ShuffleState.Off)
            send('ACK:SHUFFLE_TOGGLE')
        elif cmd == 'REPEAT_TOGGLE':
            ps = await g_atv.metadata.playing()
            rep = str(ps.repeat)
            if 'Off' in rep:
                await rc.set_repeat(RepeatState.Track)
            elif 'Track' in rep:
                await rc.set_repeat(RepeatState.All)
            else:
                await rc.set_repeat(RepeatState.Off)
            send('ACK:REPEAT_TOGGLE')
        else:
            send('ERROR:Unknown:' + cmd)
    except Exception as e:
        send('ERROR:' + cmd + ':' + str(e))

def on_data_received(data):
    try:
        cmd = str(data).strip()
        _diag('RX<' + cmd[:80])
        debug('RX:' + cmd)
        if cmd and g_loop:
            fut = asyncio.run_coroutine_threadsafe(process_cmd(cmd), g_loop)
    except Exception as e:
        _diag('RX_ERR:' + str(e))
        send('ERROR:on_data_received:' + str(e))

async def process_cmd(cmd):
    try:
        await handle_cmd(cmd)
    except Exception as e:
        send('ERROR:cmd(' + cmd + '):' + str(type(e).__name__) + ':' + str(e))

def on_subscribe_data(data):
    try:
        raw = str(data).strip()
        _diag('SUB_RAW>' + raw[:200])
        # Data arrives as JSON from SendData - extract the cmd
        cmd = raw
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict) and 'cmd' in parsed:
                cmd = parsed['cmd']
        except Exception:
            pass
        _diag('SUB_CMD>' + cmd[:80])
        on_data_received(cmd)
    except Exception as e:
        _diag('SUB_ERR>' + str(e))

def crestron_main(mod):
    global g_mod, g_loop
    g_mod = mod
    _diag('crestron_main ENTERED, mod.uid=' + str(mod.uid))

    g_loop = asyncio.new_event_loop()

    def _run_loop():
        asyncio.set_event_loop(g_loop)
        _diag('asyncio loop running')
        g_loop.run_forever()

    t = threading.Thread(target=_run_loop, daemon=True)
    t.start()

    # Register subscribe callback for incoming data from SIMPL+
    mod.subscribe(on_subscribe_data)
    _diag('subscribed')

    send('BRIDGE_READY')
    _diag('BRIDGE_READY sent, entering keepalive loop')

    # Keep crestron_main alive so module stays Running
    # Also poll get() in case data comes through that channel
    import time
    _test_cmd_file = os.path.join(SCRIPT_DIR, 'test_cmd.txt')
    while True:
        try:
            data = mod.get()
            if data:
                _diag('GET>' + str(data)[:200])
                on_subscribe_data(data)
        except Exception:
            pass
        # Test command injection for diagnostics
        try:
            if os.path.exists(_test_cmd_file):
                with open(_test_cmd_file, 'r') as f:
                    content = f.read()
                os.remove(_test_cmd_file)
                for cmd_line in content.strip().split('\n'):
                    cmd_line = cmd_line.strip()
                    if cmd_line:
                        _diag('TEST_CMD>' + cmd_line[:200])
                        on_data_received(cmd_line)
        except Exception as tce:
            _diag('TEST_CMD_ERR>' + str(tce))
        time.sleep(0.1)
