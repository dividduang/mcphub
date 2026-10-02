"""Real HTTP acceptance client. Reads MCPHUB_QA_KEY; never prints credentials."""

import argparse
import concurrent.futures
import json
import os
import socket
import time

from ipaddress import ip_address
from urllib.parse import urlsplit

import requests

VERSION = '2026-07-28'


def rpc(base, slug, token, method, params=None, *, version=VERSION, headers=None, notify=False):
    params = dict(params or {})
    if version == VERSION:
        params['_meta'] = {'io.modelcontextprotocol/protocolVersion': VERSION,
                           'io.modelcontextprotocol/clientCapabilities': {},
                           'io.modelcontextprotocol/clientInfo': {'name': 'mcphub-qa', 'version': '1'}}
    payload = {'jsonrpc': '2.0', 'method': method, 'params': params}
    if not notify:
        payload['id'] = 1
    request_headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json',
                       'Accept': 'application/json, text/event-stream'}
    if version:
        request_headers['MCP-Protocol-Version'] = version
    if version == VERSION:
        request_headers['Mcp-Method'] = method
        if method == 'tools/call':
            request_headers['Mcp-Name'] = params['name']
    request_headers.update(headers or {})
    with requests.Session() as session:
        session.trust_env = False
        response = session.post(f'{base}/mcp/{slug}', json=payload, headers=request_headers, timeout=(5, 40))
        try:
            result = response.json()
        except ValueError:
            result = {'text': response.text[:200]}
        return response.status_code, dict(response.headers), result


def loopback_origin(value):
    parts = urlsplit(value)
    if parts.scheme != 'http' or not parts.hostname or not ip_address(parts.hostname).is_loopback or not parts.port:
        raise ValueError('Capacity/cancel acceptance requires HTTP literal-loopback origins')
    return parts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['discover', 'list', 'call', 'legacy', 'capacity', 'cancel'])
    parser.add_argument('--base', default='http://127.0.0.1:8000')
    parser.add_argument('--slug', default='pypi', choices=['pypi', 'hackernews'])
    parser.add_argument('--tool', default='pypi_versions')
    parser.add_argument('--arguments', default='{"project":"requests","limit":2}')
    parser.add_argument('--upstream', default='http://127.0.0.1:19091')
    parser.add_argument('--concurrency', type=int, default=220)
    args = parser.parse_args()
    token = os.environ['MCPHUB_QA_KEY']
    base = args.base.rstrip('/')
    arguments = json.loads(args.arguments)
    call_params = {'name': args.tool, 'arguments': arguments}
    if args.mode == 'legacy':
        status, headers, body = rpc(base, args.slug, token, 'initialize',
            {'protocolVersion': '2025-11-25', 'capabilities': {}, 'clientInfo': {'name': 'qa-legacy', 'version': '1'}}, version=None)
        assert status == 200 and body['result']['protocolVersion'] == '2025-11-25'
        sid = next(v for k, v in headers.items() if k.lower() == 'mcp-session-id')
        extra = {'Mcp-Session-Id': sid}
        assert rpc(base, args.slug, token, 'notifications/initialized', version='2025-11-25', headers=extra, notify=True)[0] == 202
        listed = rpc(base, args.slug, token, 'tools/list', version='2025-11-25', headers=extra)
        called = rpc(base, args.slug, token, 'tools/call', call_params, version='2025-11-25', headers=extra)
        print(json.dumps({'legacy_list': listed[2], 'legacy_call': called[2]}, ensure_ascii=False))
        return
    if args.mode in ('discover', 'list', 'call'):
        method = {'discover': 'server/discover', 'list': 'tools/list', 'call': 'tools/call'}[args.mode]
        status, _, body = rpc(base, args.slug, token, method, call_params if args.mode == 'call' else None)
        print(json.dumps({'http_status': status, 'body': body}, ensure_ascii=False))
        return
    parts = loopback_origin(base)
    loopback_origin(args.upstream)
    with requests.Session() as session:
        session.trust_env = False
        before = session.get(args.upstream + '/metrics', timeout=5).json()
        runtime_before = session.get(base + '/mcp/_runtime', headers={'Authorization': f'Bearer {token}'}, timeout=5)
        runtime_before.raise_for_status()  # Proves the explicit test-only runtime is enabled.
        if args.mode == 'capacity':
            if not 1 <= args.concurrency <= 512:
                parser.error('concurrency must be 1..512')
            start = time.monotonic()
            latencies = []

            def measured_call(_):
                began = time.monotonic()
                response = rpc(base, args.slug, token, 'tools/call', call_params)
                return response, time.monotonic() - began

            with concurrent.futures.ThreadPoolExecutor(args.concurrency) as pool:
                measured = list(pool.map(measured_call, range(args.concurrency)))
            results = [result for result, _ in measured]
            latencies = sorted(elapsed for _, elapsed in measured)
            successful = sum(status == 200 and 'result' in body and not body['result'].get('isError') for status, _, body in results)
            upstream = session.get(args.upstream + '/metrics', timeout=5).json()
            runtime = session.get(base + '/mcp/_runtime', headers={'Authorization': f'Bearer {token}'}, timeout=5).json()
            print(json.dumps({'concurrency': args.concurrency, 'successful': successful,
                              'elapsed_seconds': round(time.monotonic() - start, 3),
                              'success_rate': successful / args.concurrency,
                              'p95_seconds': round(latencies[(len(latencies) * 95 - 1) // 100], 3),
                              'p99_seconds': round(latencies[(len(latencies) * 99 - 1) // 100], 3),
                              'upstream_before': before, 'upstream_after': upstream, 'runtime': runtime}))
            assert upstream['peak'] >= 200 and runtime['peak_active_requests'] >= 200
            assert successful == args.concurrency and upstream['active'] == 0
            assert runtime['active_requests'] == 0 and runtime['admitted'] == 0
            return
        body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
            **call_params, '_meta': {'io.modelcontextprotocol/protocolVersion': VERSION,
                                     'io.modelcontextprotocol/clientCapabilities': {}}}}).encode()
        wire_headers = (f'POST /mcp/{args.slug} HTTP/1.1\r\nHost: {parts.netloc}\r\nAuthorization: Bearer {token}\r\n'
                        f'Content-Type: application/json\r\nAccept: application/json, text/event-stream\r\n'
                        f'MCP-Protocol-Version: {VERSION}\r\nMcp-Method: tools/call\r\nMcp-Name: {args.tool}\r\n'
                        f'Content-Length: {len(body)}\r\n\r\n').encode()
        with socket.create_connection((parts.hostname, parts.port), timeout=5) as sock:
            sock.sendall(wire_headers + body)
            deadline = time.monotonic() + 5
            while session.get(args.upstream + '/metrics', timeout=5).json()['active'] == 0:
                if time.monotonic() > deadline:
                    raise RuntimeError('No real upstream execution observed')
                time.sleep(.02)
        status, _, denied = rpc(base, args.slug, token, 'tools/call', call_params)
        during = session.get(args.upstream + '/metrics', timeout=5).json()
        print(json.dumps({'after_disconnect': during, 'replacement_http_status': status, 'replacement_body': denied}))
        assert during['hits'] == before['hits'] + 1
        assert status != 200 or 'error' in denied or denied.get('result', {}).get('isError')
        deadline = time.monotonic() + 20
        while session.get(args.upstream + '/metrics', timeout=5).json()['active']:
            if time.monotonic() > deadline:
                raise RuntimeError('Upstream did not drain')
            time.sleep(.05)
        status, _, after = rpc(base, args.slug, token, 'tools/call', call_params)
        assert status == 200 and not after['result'].get('isError')
        print(json.dumps({'after_real_exit': 'replacement permitted', 'body': after}))


if __name__ == '__main__':
    main()
