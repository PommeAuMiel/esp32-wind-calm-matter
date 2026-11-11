#!/usr/bin/env python3
"""Capture the light diagnostic over USB; only readback/marker commands are allowed."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import time

ROOT = Path(__file__).resolve().parents[1]
CAPTURES = ROOT / 'build/light-diagnostics'
PREFIXES = ('STATUS:', 'TUYA:', 'LIGHT_SYNC:', 'LIGHT_DETAILS:', 'FAN_DETAILS:',
            'DIAG:', 'DIAG_QUERY:', 'DIAG_MARK:', 'FRAME_DIAG:', 'DP_DIAG:',
            'ERROR:', 'ENDPOINTS:', 'RESET:')
PANIC = re.compile(r'Guru Meditation|assert failed|abort\(\)|Backtrace:|panic\b', re.I)
LABEL = re.compile(r'[A-Za-z0-9_-]{1,32}\Z')


def write_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def signal(args):
    folder = CAPTURES / args.capture
    if folder.parent != CAPTURES or not (folder / 'state.json').is_file():
        raise SystemExit('Unknown capture')
    if args.action == 'mark' and not LABEL.fullmatch(args.label or ''):
        raise SystemExit('Marker must contain 1..32 ASCII letters, digits, underscore or dash')
    state = json.loads((folder / 'state.json').read_text())
    if not state['running']:
        raise SystemExit('Capture has finished')
    with (folder / 'commands.jsonl').open('a') as stream:
        stream.write(json.dumps({'action': args.action, 'label': args.label}) + '\n')


def capture(args):
    import serial
    import serial.tools.list_ports
    ports = [p for p in serial.tools.list_ports.comports() if p.device == args.port]
    if len(ports) != 1 or (ports[0].vid, ports[0].pid, ports[0].serial_number) != (
            0x303a, 0x1001, args.expected_id):
        raise SystemExit('USB identity mismatch')
    if not 30 <= args.seconds <= 900:
        raise SystemExit('Capture duration must be 30..900 seconds')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    folder = CAPTURES / stamp
    folder.mkdir(mode=0o700, parents=True)
    private = ROOT / 'private/diagnostic-captures' / stamp
    private.mkdir(mode=0o700, parents=True)
    command_path = folder / 'commands.jsonl'
    command_path.touch(mode=0o600)
    state = {'running': True, 'port': args.port, 'usb_serial': args.expected_id,
             'duration_limit_seconds': args.seconds, 'elapsed_seconds': 0,
             'last_status': None, 'last_tuya': None, 'last_light_sync': None,
             'panics': 0, 'app_errors': 0, 'readback_requests': 0,
             'actuation_commands_sent_by_capture': False}
    write_json(folder / 'state.json', state)
    usb = serial.Serial(port=None, baudrate=115200, timeout=0.1, write_timeout=1)
    usb.dtr = False
    usb.rts = False
    usb.port = args.port
    usb.open()
    print('CAPTURE_STARTED: ' + stamp, flush=True)
    start = time.monotonic()
    pending = b''
    enabled = False
    next_enable = start
    next_state = start
    stop = False
    with (private / 'raw.log').open('xb') as raw, (folder / 'events.jsonl').open('x') as events, \
            command_path.open() as commands:
        def send(command):
            usb.write((command + '\n').encode('ascii'))
            events.write(json.dumps({'host_elapsed_ms': round((time.monotonic()-start)*1000, 1),
                                     'host_command': command}) + '\n')
            events.flush()
        try:
            while not stop and time.monotonic() - start < args.seconds:
                now = time.monotonic()
                if now >= next_enable:
                    send('diag on')
                    # First command can precede application startup when opening CDC resets it.
                    next_enable = now + (90 if enabled else 3)
                for line in commands.readlines():
                    command = json.loads(line)
                    action = command['action']
                    if action == 'stop':
                        stop = True
                    elif action in ('snapshot', 'query'):
                        send('diag ' + action)
                        if action == 'query':
                            state['readback_requests'] += 1
                    elif action == 'mark' and LABEL.fullmatch(command.get('label') or ''):
                        send('diag mark ' + command['label'])
                    else:
                        raise ValueError('Invalid capture command')
                data = usb.read(4096)
                if data:
                    raw.write(data)
                    raw.flush()
                    pending += data
                    while b'\n' in pending:
                        part, pending = pending.split(b'\n', 1)
                        line = part.decode('utf-8', 'replace').strip()
                        if line.startswith(PREFIXES) or PANIC.search(line):
                            events.write(json.dumps({'host_elapsed_ms': round((time.monotonic()-start)*1000, 1),
                                                     'line': line}) + '\n')
                            events.flush()
                            for prefix, key in (('STATUS:', 'last_status'), ('TUYA:', 'last_tuya'),
                                                ('LIGHT_SYNC:', 'last_light_sync')):
                                if line.startswith(prefix):
                                    state[key] = line
                            if line.startswith('DIAG: enabled=1'):
                                enabled = True
                                next_enable = time.monotonic() + 90
                            state['panics'] += bool(PANIC.search(line))
                            state['app_errors'] += line.startswith('ERROR:')
                            if line.startswith(('DP_DIAG:', 'DIAG_QUERY:', 'DIAG_MARK:', 'ERROR:', 'ENDPOINTS:')) \
                                    or PANIC.search(line):
                                print(line, flush=True)
                    if len(pending) > 65536:
                        pending = pending[-65536:]
                if now >= next_state:
                    state['elapsed_seconds'] = round(time.monotonic() - start, 1)
                    write_json(folder / 'state.json', state)
                    next_state = now + 1
        finally:
            try:
                send('diag off')
            finally:
                usb.close()
                state['running'] = False
                state['elapsed_seconds'] = round(time.monotonic() - start, 1)
                write_json(folder / 'state.json', state)
    print(json.dumps(state, indent=2), flush=True)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='operation', required=True)
    start = sub.add_parser('start')
    start.add_argument('--port', required=True)
    start.add_argument('--expected-id', required=True)
    start.add_argument('--seconds', type=int, default=360)
    control = sub.add_parser('signal')
    control.add_argument('--capture', required=True)
    control.add_argument('--action', choices=('snapshot', 'query', 'mark', 'stop'), required=True)
    control.add_argument('--label')
    args = parser.parse_args()
    (capture if args.operation == 'start' else signal)(args)


if __name__ == '__main__':
    main()
