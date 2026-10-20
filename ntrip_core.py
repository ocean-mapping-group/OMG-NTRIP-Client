#!/usr/bin/env python3
"""NTRIP RTCM logger for OxTS xNAV650, with optional serial forwarding.

Requires: pip install pyserial

Modes:
  bridge: read receiver GGA over serial, send to caster, log RTCM, and forward RTCM to receiver.
  logger: read receiver GGA over serial, send to caster, and log RTCM only.

For VRS, the receiver must output NMEA GGA on the selected serial port.
"""
import argparse
import base64
import csv
import datetime as dt
import os
import socket
import ssl
import sys
import time
from pathlib import Path

import serial
import math
import select
import json
from collections import Counter
from serial.tools import list_ports


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='milliseconds')


def read_header(sock):
    header = bytearray()
    while not (header.endswith(b'\r\n\r\n') or header.endswith(b'\n\n')):
        b = sock.recv(1)
        if not b:
            raise ConnectionError('Caster closed before response header')
        header.extend(b)
        if len(header) > 16384:
            raise ValueError('Oversized caster response header')
    first = bytes(header).splitlines()[0].decode('latin1', 'replace')
    if not first.startswith(('ICY 200', 'HTTP/1.0 200', 'HTTP/1.1 200')):
        raise ConnectionError(f'Caster response: {first}')
    if b'transfer-encoding: chunked' in header.lower():
        raise ValueError('Chunked HTTP transfer is not supported; use an NTRIP v1 raw stream')
    return first


def open_caster(args):
    sock = socket.create_connection((args.host, args.port), timeout=args.connect_timeout)
    try:
        if args.tls:
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=args.host)
        token = base64.b64encode(f'{args.user}:{args.password}'.encode()).decode('ascii')
        auth = f'Authorization: Basic {token}\r\n' if args.user else ''
        req = (f'GET /{args.mount.lstrip("/")} HTTP/1.0\r\n'
               'User-Agent: NTRIP RTCMBridge/2.0\r\n'
               'Accept: */*\r\nConnection: close\r\n'
               f'{auth}\r\n')
        sock.sendall(req.encode('ascii'))
        status = read_header(sock)
        sock.settimeout(0.25)
        return sock, status
    except Exception:
        sock.close()
        raise


def fetch_mountpoints(host, port, user='', password='', tls=False, timeout=15):
    """Retrieve advertised NTRIP sourcetable STR records (not necessarily exhaustive)."""
    if not host.strip():
        raise ValueError('Enter a caster hostname or IP address')
    if not (1 <= int(port) <= 65535):
        raise ValueError('Caster port must be between 1 and 65535')
    raw = socket.create_connection((host.strip(), int(port)), timeout=float(timeout))
    try:
        sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host.strip()) if tls else raw
        with sock:
            sock.settimeout(float(timeout))
            token = base64.b64encode(f'{user}:{password}'.encode('utf-8')).decode('ascii')
            auth = f'Authorization: Basic {token}\r\n' if user else ''
            req = ('GET / HTTP/1.0\r\n'
                   'User-Agent: NTRIP xNAV650-MountpointBrowser/1.0\r\n'
                   'Accept: */*\r\nConnection: close\r\n'
                   f'{auth}\r\n')
            sock.sendall(req.encode('ascii'))
            chunks = bytearray()
            limit = 2 * 1024 * 1024
            while len(chunks) < limit:
                try: part = sock.recv(min(16384, limit - len(chunks)))
                except socket.timeout:
                    if chunks: break
                    raise TimeoutError('Caster did not return a sourcetable in time')
                if not part: break
                chunks.extend(part)
                if b'ENDSOURCETABLE' in chunks.upper(): break
            if len(chunks) >= limit:
                raise ValueError('Sourcetable exceeded 2 MB safety limit')
    finally:
        raw.close()
    body = bytes(chunks).decode('latin-1', errors='replace')
    first = body.splitlines()[0].strip() if body else ''
    if first.startswith(('HTTP/1.0 401', 'HTTP/1.1 401', 'HTTP/1.0 403', 'HTTP/1.1 403')):
        raise PermissionError(f'Caster rejected credentials: {first}')
    if not first.startswith(('SOURCETABLE 200', 'HTTP/1.0 200', 'HTTP/1.1 200', 'ICY 200')):
        raise ConnectionError(f'Unexpected caster response: {first[:180]}')
    records = []
    for line in body.splitlines():
        if not line.startswith('STR;'): continue
        cols = line.strip().split(';')
        if len(cols) < 2 or not cols[1].strip(): continue
        records.append({'mount': cols[1].strip(), 'identifier': cols[2] if len(cols)>2 else '',
                        'format': cols[3] if len(cols)>3 else '',
                        'nav': cols[6] if len(cols)>6 else '',
                        'network': cols[7] if len(cols)>7 else '',
                        'country': cols[8] if len(cols)>8 else '',
                        'nmea': cols[11] if len(cols)>11 else '',
                        'auth': cols[15] if len(cols)>15 else '',
                        'fee': cols[16] if len(cols)>16 else ''})
    return records


def make_static_gga(latitude, longitude, altitude):
    """Build a checksummed NMEA GGA from decimal degrees and ellipsoidal height.

    The supplied altitude is encoded as an approximate MSL height with zero
    geoid separation; for VRS positioning the horizontal coordinates matter.
    """
    now = dt.datetime.now(dt.timezone.utc)
    def coord(value, latitude_axis):
        hemi = ('N' if value >= 0 else 'S') if latitude_axis else ('E' if value >= 0 else 'W')
        absolute = abs(value)
        deg = int(absolute)
        mins = (absolute - deg) * 60
        return f'{deg:02d}{mins:010.7f}' if latitude_axis else f'{deg:03d}{mins:010.7f}', hemi
    lat, ns = coord(latitude, True)
    lon, ew = coord(longitude, False)
    body = f'GPGGA,{now:%H%M%S}.00,{lat},{ns},{lon},{ew},1,12,1.0,{altitude:.3f},M,0.0,M,,'
    checksum = 0
    for ch in body.encode('ascii'):
        checksum ^= ch
    return f'${body}*{checksum:02X}\r\n'.encode('ascii')


class RTCMInspector:
    """Streaming RTCM3 frame validator; handles frames split across recv calls."""
    def __init__(self):
        self.pending = bytearray()
        self.valid = 0
        self.bad_crc = 0
        self.types = Counter()

    @staticmethod
    def crc24q(data):
        crc = 0
        for byte in data:
            crc ^= byte << 16
            for _ in range(8):
                crc = ((crc << 1) ^ (0x1864CFB if crc & 0x800000 else 0)) & 0xFFFFFF
        return crc

    def feed(self, block):
        self.pending.extend(block)
        while len(self.pending) >= 6:
            if self.pending[0] != 0xD3:
                del self.pending[0]
                continue
            if self.pending[1] & 0xFC:
                del self.pending[0]
                continue
            size = ((self.pending[1] & 3) << 8) | self.pending[2]
            length = size + 6
            if len(self.pending) < length:
                break
            frame = self.pending[:length]
            expected = int.from_bytes(frame[-3:], 'big')
            if self.crc24q(frame[:-3]) == expected:
                self.valid += 1
                if size >= 2:
                    msg = (frame[3] << 4) | (frame[4] >> 4)
                    self.types[msg] += 1
                del self.pending[:length]
            else:
                self.bad_crc += 1
                del self.pending[0]
        if len(self.pending) > 2048:
            self.pending = self.pending[-2048:]


def gga_details(sentence):
    try:
        fields = sentence.decode('ascii', 'replace').strip().split(',')
        if len(fields) < 10:
            return {}
        def decimal(value, hemisphere):
            if not value: return None
            degree_count = 2 if hemisphere in ('N','S') else 3
            number = float(value[:degree_count]) + float(value[degree_count:]) / 60
            return -number if hemisphere in ('S','W') else number
        return dict(latitude=decimal(fields[2],fields[3]), longitude=decimal(fields[4],fields[5]),
                    fix=int(fields[6] or 0), satellites=int(fields[7] or 0),
                    altitude=float(fields[9]) if fields[9] else None)
    except (ValueError, IndexError):
        return {}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--mode', choices=('bridge', 'logger'), required=True,
                   help='bridge forwards corrections to xNAV650; logger saves them only')
    p.add_argument('--host', required=True)
    p.add_argument('--port', type=int, default=2101)
    p.add_argument('--mount', required=True)
    p.add_argument('--user', default='')
    p.add_argument('--password', default='')
    p.add_argument('--serial', default='', help='COM port receiving xNAV650 NMEA GGA')
    p.add_argument('--static-position', action='store_true', help='Use fixed GGA coordinates instead of serial input (logger mode only)')
    p.add_argument('--latitude', type=float)
    p.add_argument('--longitude', type=float)
    p.add_argument('--altitude', type=float, default=0.0)
    p.add_argument('--baud', type=int, default=115200)
    p.add_argument('--serial-retry-seconds', type=float, default=5)
    p.add_argument('--serial-hwid', default='', help='Optional USB serial number or VID:PID to follow COM renumbering')
    p.add_argument('--gga-interval', type=float, default=10)
    p.add_argument('--log-dir', default='NTRIP_Logs')
    p.add_argument('--tls', action='store_true')
    p.add_argument('--no-file', action='store_true', help='Disable RTCM file recording')
    p.add_argument('--forward-input', action='store_true', help='Forward RTCM over receiver input COM port')
    p.add_argument('--output-serial', default='', help='Additional RTCM output COM port')
    p.add_argument('--output-baud', type=int, default=115200)
    p.add_argument('--tcp-listen', action='store_true', help='Serve raw RTCM to TCP clients')
    p.add_argument('--tcp-bind', default='0.0.0.0')
    p.add_argument('--tcp-port', type=int, default=2102)
    p.add_argument('--udp-send', action='store_true', help='Send raw RTCM over UDP')
    p.add_argument('--udp-host', default='255.255.255.255')
    p.add_argument('--udp-port', type=int, default=2103)
    p.add_argument('--rotate', choices=('connection','hourly','size'), default='connection')
    p.add_argument('--rotate-mb', type=float, default=100.0)

    p.add_argument('--retry-seconds', type=float, default=5)
    p.add_argument('--max-retry-seconds', type=float, default=60)
    p.add_argument('--connect-timeout', type=float, default=15)
    p.add_argument('--idle-timeout', type=float, default=30,
                   help='Reconnect if caster sends no RTCM for this many seconds; 0 disables')
    p.add_argument('--gga-timeout', type=float, default=30,
                   help='Warn if receiver GGA has not arrived for this many seconds; 0 disables')
    a = p.parse_args(argv)
    if a.rotate_mb <= 0: p.error('Rotation size must be positive')
    if a.gga_interval <= 0 or a.retry_seconds <= 0 or a.max_retry_seconds <= 0 or a.serial_retry_seconds <= 0:
        p.error('GGA interval and retry intervals must be positive')
    if a.static_position:
        if a.mode == 'bridge' or a.forward_input:
            p.error('Input COM forwarding requires serial position input')
        if a.latitude is None or a.longitude is None or not (-90 <= a.latitude <= 90) or not (-180 <= a.longitude <= 180):
            p.error('Static latitude (-90..90) and longitude (-180..180) are required')
        if not math.isfinite(a.latitude) or not math.isfinite(a.longitude) or not math.isfinite(a.altitude):
            p.error('Static position must contain finite numbers')
    elif not a.serial.strip():
        p.error('COM port required unless static position is selected')
    if a.mode == 'bridge':
        a.forward_input = True
    if a.output_serial and not a.static_position and a.output_serial.upper() == a.serial.upper():
        p.error('Additional output COM port must differ from receiver input COM port')
    if not (1 <= a.tcp_port <= 65535 and 1 <= a.udp_port <= 65535 and a.output_baud > 0):
        p.error('Output ports must be 1..65535 and output baud positive')
    if not (not a.no_file or a.forward_input or a.output_serial or a.tcp_listen or a.udp_send):
        p.error('Select at least one RTCM output')
    Path(a.log_dir).mkdir(parents=True, exist_ok=True)
    event_path = Path(a.log_dir) / 'connection_events.csv'
    events_file = open(event_path, 'a', newline='', encoding='utf-8')
    events = csv.writer(events_file)
    if events_file.tell() == 0:
        events.writerow(['utc', 'event', 'detail'])
        events_file.flush()

    def event(kind, detail):
        print(f'{utc()} [{kind}] {detail}', flush=True)
        events.writerow([utc(), kind, str(detail)])
        events_file.flush()

    extra_serial = None
    next_output_retry = 0.0
    tcp_server = None
    tcp_clients = []
    udp_socket = None
    if a.tcp_listen:
        tcp_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        tcp_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            tcp_server.bind((a.tcp_bind, a.tcp_port))
            tcp_server.listen(8)
            tcp_server.setblocking(False)
        except OSError as exc:
            p.error(f'Cannot open TCP listener: {exc}')
    if a.udp_send:
        udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    receiver = None
    next_serial_retry = 0.0
    serial_connected_once = False
    active_serial = a.serial

    def serial_name():
        if not a.serial_hwid.strip():
            return a.serial
        key = a.serial_hwid.strip().lower()
        matches = [port.device for port in list_ports.comports()
                   if key in ((port.hwid or '') + ' ' + (port.serial_number or '')).lower()]
        if len(matches) > 1:
            raise OSError(f'Multiple COM devices match USB identity {a.serial_hwid!r}: {matches}')
        if not matches:
            raise OSError(f'USB serial identity {a.serial_hwid!r} not present')
        return matches[0]
    gga_buffer = bytearray()
    latest_gga = None
    last_gga_received = None
    warned_no_gga = False
    attempt = 0
    connection_num = 0
    next_retry = 0.0
    sock = None
    log = None
    total_received = 0
    total_logged = 0
    total_messages = 0
    last_telemetry = 0.0
    inspector = RTCMInspector()
    out_counts = dict(input=0, serial=0, tcp=0, udp=0)
    out_errors = dict(input=0, serial=0, tcp=0, udp=0)
    latest_position = {}
    previous_telemetry_rx = 0
    previous_telemetry_time = time.monotonic()
    file_hour = None
    current_path = None
    file_sequence = 0
    last_fsync = time.monotonic()

    def open_segment():
        nonlocal log, current_path, file_hour, file_sequence
        if a.no_file:
            return
        if log:
            log.close()
        file_sequence += 1
        stamp = dt.datetime.now(dt.timezone.utc)
        file_hour = stamp.strftime('%Y%m%d_%H')
        current_path = Path(a.log_dir) / f'RTCM_{stamp:%Y%m%d_%H%M%S_%f}_{file_sequence:04d}.rtcm3'
        log = open(current_path, 'xb', buffering=0)
        event('file_opened', str(current_path))

    try:
        event('startup', f'mode={a.mode}, caster={a.host}:{a.port}/{a.mount}, position_source={"static" if a.static_position else "serial"}, serial={a.serial}@{a.baud}')
        if tcp_server:
            event('tcp_listening', f'{a.tcp_bind}:{a.tcp_port}')
        if udp_socket:
            event('udp_output', f'{a.udp_host}:{a.udp_port}')
        while True:
            if a.output_serial and extra_serial is None and time.monotonic() >= next_output_retry:
                try:
                    extra_serial = serial.Serial(a.output_serial, a.output_baud, timeout=0, write_timeout=1)
                    event('output_serial_connected', f'{a.output_serial}@{a.output_baud}')
                except (OSError, serial.SerialException) as exc:
                    next_output_retry = time.monotonic() + a.serial_retry_seconds
                    event('output_serial_wait', str(exc))
            if tcp_server:
                try:
                    while True:
                        client, addr = tcp_server.accept()
                        client.setblocking(False)
                        tcp_clients.append(client)
                        event('tcp_client_connected', str(addr))
                except BlockingIOError:
                    pass
            if receiver is None and not a.static_position:
                if time.monotonic() < next_serial_retry:
                    time.sleep(0.1)
                    continue
                try:
                    active_serial = serial_name()
                    receiver = serial.Serial(active_serial, a.baud, timeout=0, write_timeout=5)
                    receiver.reset_input_buffer()
                    gga_buffer.clear()
                    latest_gga = None
                    last_gga_received = None
                    warned_no_gga = False
                    event('serial_connected', f'{active_serial}@{a.baud}')
                    serial_connected_once = True
                except (OSError, serial.SerialException) as exc:
                    next_serial_retry = time.monotonic() + a.serial_retry_seconds
                    event('serial_wait', f'{exc}; retry in {a.serial_retry_seconds:g}s')
                    time.sleep(0.1)
                    continue
            try:
                # Keep polling GGA even while the caster is disconnected.
                available = 0 if a.static_position else receiver.in_waiting
                if available:
                    gga_buffer.extend(receiver.read(min(available, 8192)))
                    while b'\n' in gga_buffer:
                        line, _, remainder = gga_buffer.partition(b'\n')
                        gga_buffer = bytearray(remainder)
                        line = line.strip()
                        if line.startswith((b'$GPGGA,', b'$GNGGA,')) and len(line) < 512:
                            latest_gga = line + b'\r\n'
                            latest_position = gga_details(latest_gga)
                            last_gga_received = time.monotonic()
                            warned_no_gga = False
                    if len(gga_buffer) > 8192:
                        gga_buffer.clear()
                now = time.monotonic()
                if a.static_position:
                    latest_gga = make_static_gga(a.latitude, a.longitude, a.altitude)
                    latest_position = gga_details(latest_gga)
                    last_gga_received = now
                if a.gga_timeout and (last_gga_received is None or now - last_gga_received > a.gga_timeout) and not warned_no_gga:
                    event('warning', 'No recent receiver GGA; VRS caster may not provide corrections')
                    warned_no_gga = True
                if sock is None and now >= next_retry:
                    try:
                        sock, status = open_caster(a)
                        connection_num += 1
                        attempt = 0
                        if not a.no_file and (log is None or a.rotate == 'connection'):
                            open_segment()
                        event('connected', f'{status}; file={current_path}')
                        last_rx = time.monotonic()
                        last_gga_sent = 0.0
                        last_report = last_rx
                        bytes_received = 0
                    except (OSError, ConnectionError, ValueError, ssl.SSLError) as exc:
                        if sock:
                            sock.close()
                            sock = None
                        if log and a.rotate == 'connection':
                            log.close()
                            log = None
                        attempt += 1
                        delay = min(a.retry_seconds * (2 ** min(attempt - 1, 12)), a.max_retry_seconds)
                        next_retry = time.monotonic() + delay
                        event('connect_failed', f'{exc}; retry in {delay:g}s')
                if sock is not None:
                    try:
                        now = time.monotonic()
                        if latest_gga is not None and now - last_gga_sent >= a.gga_interval:
                            # Only transmit recent positions, never a stale GGA fix.
                            if last_gga_received is not None and now - last_gga_received <= max(a.gga_interval * 3, 30):
                                if a.static_position:
                                    latest_gga = make_static_gga(a.latitude, a.longitude, a.altitude)
                                sock.sendall(latest_gga)
                                last_gga_sent = now
                        try:
                            block = sock.recv(65536)
                        except socket.timeout:
                            block = None
                        if block == b'':
                            raise ConnectionError('Caster closed the connection')
                        if block:
                            last_rx = time.monotonic()
                            inspector.feed(block)
                            if log and a.rotate == 'hourly' and dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d_%H') != file_hour:
                                open_segment()
                            if log and a.rotate == 'size' and log.tell() >= a.rotate_mb * 1024 * 1024:
                                open_segment()
                            # Log all received correction bytes, independently of serial delivery.
                            if log:
                                written_to_file = log.write(block)
                                if written_to_file != len(block):
                                    raise OSError("Incomplete RTCM file write")
                                total_logged += written_to_file
                            bytes_received += len(block)
                            total_received += len(block)
                            total_messages += 1
                            if extra_serial:
                                try:
                                    written = extra_serial.write(block)
                                    out_counts["serial"] += written
                                    if written != len(block):
                                        raise serial.SerialException('Short write on extra output COM')
                                except (OSError, serial.SerialException) as exc:
                                    out_errors['serial'] += 1
                                    event('output_serial_disconnected', str(exc))
                                    try: extra_serial.close()
                                    except OSError: pass
                                    extra_serial = None
                                    next_output_retry = time.monotonic() + a.serial_retry_seconds
                            if udp_socket:
                                try:
                                    # UDP datagrams are bounded; split RTCM byte stream into <=1200-byte packets.
                                    for offset in range(0, len(block), 1200):
                                        out_counts["udp"] += udp_socket.sendto(block[offset:offset+1200], (a.udp_host, a.udp_port))
                                except OSError as exc:
                                    out_errors['udp'] += 1
                                    event('udp_send_error', str(exc))
                            for client in tcp_clients[:]:
                                try:
                                    # Non-blocking send: disconnect slow clients rather than stall logging.
                                    sent = client.send(block)
                                    if sent != len(block):
                                        raise OSError('Slow TCP client / incomplete write')
                                    out_counts['tcp'] += sent
                                except (OSError, BlockingIOError) as exc:
                                    try: client.close()
                                    except OSError: pass
                                    tcp_clients.remove(client)
                                    out_errors['tcp'] += 1
                                    event('tcp_client_disconnected', str(exc))
                            if a.forward_input:
                                try:
                                    written = receiver.write(block)
                                except OSError as exc:
                                    raise serial.SerialException(str(exc)) from exc
                                if written != len(block):
                                    raise serial.SerialException(f'Short serial write: {written}/{len(block)}')
                                out_counts['input'] += written
                        now = time.monotonic()
                        if a.idle_timeout and now - last_rx > a.idle_timeout:
                            raise TimeoutError(f'No correction bytes for {a.idle_timeout:g}s')
                        if log and now - last_fsync >= 10:
                            os.fsync(log.fileno())
                            last_fsync = now
                        if now - last_telemetry >= 1.0:
                            # Unbuffered file writes; fstat reflects the size of the open file.
                            file_size = os.fstat(log.fileno()).st_size if log else 0
                            age = now - last_rx
                            print(f'{utc()} [telemetry] received={total_received} logged={total_logged} file_size={file_size} blocks={total_messages} age={age:.1f} file_enabled={int(log is not None)}', flush=True)
                            elapsed = max(0.01, now - previous_telemetry_time)
                            rate = (total_received - previous_telemetry_rx) / elapsed
                            previous_telemetry_rx = total_received
                            previous_telemetry_time = now
                            report = dict(valid=inspector.valid, bad_crc=inspector.bad_crc,
                                types=dict(inspector.types.most_common(20)), rate=round(rate, 1),
                                gga=latest_position, gga_age=round(now-last_gga_received, 1) if last_gga_received else None,
                                outputs=out_counts, errors=out_errors, tcp_clients=len(tcp_clients),
                                serial_output_connected=bool(extra_serial), file=str(current_path or ''),
                                file_size=file_size, received=total_received, logged=total_logged)
                            print('[metrics_json] ' + json.dumps(report, separators=(',', ':')), flush=True)
                            last_telemetry = now
                        if now - last_report >= 10:
                            event('status', f'mode={a.mode}, received={bytes_received:,} bytes, GGA={"yes" if last_gga_received and now-last_gga_received < 30 else "NO"}')
                            last_report = now
                    except (OSError, ConnectionError, TimeoutError, ssl.SSLError) as exc:
                        event('disconnected', str(exc))
                        sock.close()
                        sock = None
                        if log and a.rotate == 'connection':
                            log.close()
                            log = None
                        attempt += 1
                        delay = min(a.retry_seconds * (2 ** min(attempt - 1, 12)), a.max_retry_seconds)
                        next_retry = time.monotonic() + delay
                        event('retry', f'in {delay:g}s')
                else:
                    time.sleep(0.05)
            except (OSError, serial.SerialException) as exc:
                event('serial_disconnected', f'{active_serial}: {exc}')
                if receiver:
                    try: receiver.close()
                    except OSError: pass
                receiver = None
                gga_buffer.clear()
                latest_gga = None
                last_gga_received = None
                if sock:
                    try: sock.close()
                    except OSError: pass
                    sock = None
                if log:
                    log.close()
                    log = None
                next_serial_retry = time.monotonic() + a.serial_retry_seconds
                event('serial_retry', f'in {a.serial_retry_seconds:g}s')
    except KeyboardInterrupt:
        event('stopped', 'Stopped by user')
    except (OSError, serial.SerialException) as exc:
        event('fatal_error', f'Serial device or file error: {exc}')
        raise SystemExit(1)
    finally:
        if sock:
            sock.close()
        if log:
            log.close()
        if receiver:
            receiver.close()
        if extra_serial:
            extra_serial.close()
        for client in tcp_clients:
            client.close()
        if tcp_server:
            tcp_server.close()
        if udp_socket:
            udp_socket.close()
        events_file.close()


if __name__ == '__main__':
    main()
