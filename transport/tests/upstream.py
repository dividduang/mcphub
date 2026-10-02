"""Controlled local upstream for QA only; never mounted by the production plugin."""

import argparse
import json
import threading
import time

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class ControlledUpstream(ThreadingHTTPServer):
    request_queue_size = 1024
    daemon_threads = True

    def __init__(self, address, delay=0.0):
        super().__init__(address, Handler)
        self.delay = delay
        self.guard = threading.Lock()
        self.active = self.peak = self.hits = self.completed = 0
        self.started = threading.Event()
        self.release = None
        self.redirect = False

    def counters(self):
        with self.guard:
            return {'active': self.active, 'peak': self.peak, 'hits': self.hits, 'completed': self.completed}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == '/metrics':
            self.respond(self.server.counters())
            return
        with self.server.guard:
            self.server.active += 1
            self.server.hits += 1
            self.server.peak = max(self.server.peak, self.server.active)
        self.server.started.set()
        try:
            if self.server.release is not None:
                self.server.release.wait(10)
            else:
                time.sleep(self.server.delay)
            if self.server.redirect:
                self.send_response(302)
                self.send_header('Location', 'http://169.254.169.254/latest/meta-data/')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            if self.path.startswith('/simple/'):
                data = {'name': 'controlled-project', 'versions': ['1.0.0', '2.0.0', '2.1.0']}
            elif self.path.startswith('/pypi/'):
                data = {
                    'info': {'name': 'controlled-project', 'version': '2.1.0', 'summary': 'Owned QA package metadata',
                             'requires_python': '>=3.10', 'requires_dist': ['requests>=2.34.2'],
                             'provides_extra': ['test'], 'project_urls': {'Source': 'https://example.invalid/repo'},
                             'yanked': False},
                    'urls': [{'filename': 'controlled_project-2.1.0-py3-none-any.whl', 'size': 1234,
                              'packagetype': 'bdist_wheel', 'digests': {'sha256': '0' * 64}, 'yanked': False}],
                    'vulnerabilities': [],
                }
            elif self.path.startswith('/v0/user/'):
                data = {'id': 'controlled-author', 'karma': 42, 'created': 1600000000, 'submitted': [101, 102, 103]}
            elif self.path.startswith('/v0/item/'):
                data = {'id': 101, 'type': 'story', 'title': 'Controlled story', 'score': 42, 'kids': [102, 103]}
            elif self.path.startswith('/v0/'):
                data = [101, 102, 103, 104]
            else:
                self.send_error(404)
                return
            self.respond(data)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with self.server.guard:
                self.server.active -= 1
                self.server.completed += 1

    def respond(self, data):
        payload = json.dumps(data).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=19091)
    parser.add_argument('--delay', type=float, default=4.0)
    args = parser.parse_args()
    if not 0 <= args.delay <= 10:
        parser.error('delay must be 0..10 seconds')
    server = ControlledUpstream(('127.0.0.1', args.port), args.delay)
    print(f'Controlled upstream listening on 127.0.0.1:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
