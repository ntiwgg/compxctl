"""Black-box CLI checks. Python is a development dependency, never runtime."""
import json
import os
import subprocess
import sys

binary = os.path.abspath(sys.argv[1])
checks = 0

def run(*args, code=0, demo=True):
    global checks
    p = subprocess.run([binary, *(['--demo'] if demo else []), *args], text=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
    assert p.returncode == code, (args, p.returncode, p.stdout, p.stderr)
    assert 'AddressSanitizer' not in p.stderr and 'runtime error:' not in p.stderr, p.stderr
    checks += 1
    return p

assert '5.0.0-dev' in run('--version').stdout
assert 'Commands:' in run('--help').stdout
snapshot = json.loads(run('status', '--json').stdout)
assert snapshot['battery']['percent'] == 87
assert snapshot['polling']['measured_rate_hz'] is None
assert not snapshot['polling']['application_verified']
assert len(snapshot['dpi_slots']) == 8
assert json.loads(run('devices', '--json').stdout)['devices'][0]['id'] == 'demo'
assert json.loads(run('battery', '--json').stdout)['battery']['percent'] == 87
assert 'DPI' in run('dpi', 'list').stdout
assert 'unknown' in run('capabilities').stdout.lower()
assert 'rgb' in json.loads(run('capabilities', '--json').stdout)['unimplemented_protocol']
changed = json.loads(run('dpi', 'set', '1', '1600', '--confirm', '--json').stdout)
assert changed['changed'] and changed['readback_verified'] and not changed['persistence_verified']
unchanged = json.loads(run('dpi', 'set', '1', '800', '--confirm', '--json').stdout)
assert not unchanged['write_attempted']
for args in [('dpi','set','0','800','--confirm'), ('dpi','set','1','800'),
             ('rate','host','500'), ('reset',), ('rate','device','500','--confirm')]:
    assert 'error' in json.loads(run(*args, '--json', code=1).stdout)
for args in [('dpi','set','6','800','--confirm'), ('dpi','set','1','801','--confirm'),
             ('rate','host'), ('rate','host','250','--confirm'), ('--tries','0'),
             ('--timeout-ms','99999999999999999999'), ('probe','--start','0xffff','--length','2'),
             ('probe','--length','0'), ('nonsense',), ('--device',)]:
    # Host 250 is refused by the operation (runtime validation); others are usage errors.
    code = 1 if args[:3] == ('rate','host','250') else 2
    assert 'error' in json.loads(run(*args, '--json', code=code).stdout)
for args in [('rate','host','500','--confirm'), ('rate','host','0','--confirm'),
             ('rate','device','500','--persist','--confirm'), ('reset','--confirm')]:
    assert json.loads(run(*args,'--json').stdout)['ok']
dump = json.loads(run('probe','--start','0xfff8','--length','8','--json').stdout)
assert dump['length'] == 8 and len(dump['hex']) == 16
assert run('tui', demo=False, code=2).stdout == ''
# Explicit impossible selector: no hardware required, never opens or writes a device.
missing = json.loads(run('status','--device','__not_a_usb_port__','--json',demo=False,code=1).stdout)
assert 'error' in missing
print(f'CLI: {checks} black-box cases OK')
