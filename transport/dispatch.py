import anyio
import ipaddress

from starlette.responses import JSONResponse, PlainTextResponse

from backend.plugin.mcphub.transport.server import plugin_enabled


class McpDispatcher:
    """Exact MCP routing outside FBA's JWT, body-reading and credential log layers.

    All non-MCP requests continue through the untouched FBA middleware stack.
    MCP requests receive their own strict transport and database authentication.
    """

    def __init__(self, app, hub):
        self.app, self.hub = app, hub
        self.inflight = 0

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or not (scope['path'] == '/mcp' or scope['path'].startswith('/mcp/')):
            await self.app(scope, receive, send)
            return
        # Event-loop-owned counter: no task queue before DB/body processing.
        if self.inflight >= self.hub.config.global_limit + 32:
            await PlainTextResponse('MCP request capacity exhausted', status_code=429,
                                    headers={'Retry-After': '1'})(scope, receive, send)
            return
        self.inflight += 1
        try:
            await self.handle_mcp(scope, receive, send)
        finally:
            self.inflight -= 1

    async def handle_mcp(self, scope, receive, send):
        if scope.get('query_string'):
            await PlainTextResponse('MCP query parameters are not supported', status_code=400)(scope, receive, send)
            return
        if scope['path'] == '/mcp/_runtime':
            await self.metrics(scope, receive, send)
            return
        child = self.hub.apps.get(scope['path'])
        if child is None:
            await PlainTextResponse('Not found', status_code=404)(scope, receive, send)
            return
        if not self.hub.ready or not await plugin_enabled():
            await PlainTextResponse('MCP Hub unavailable', status_code=503)(scope, receive, send)
            return
        # Reject ambiguous credentials before the framework chooses one header.
        if sum(key.lower() == b'authorization' for key, _ in scope.get('headers', ())) != 1:
            await PlainTextResponse('Bearer authentication required', status_code=401,
                                    headers={'WWW-Authenticate': 'Bearer'})(scope, receive, send)
            return
        if scope['method'] == 'POST':
            body = bytearray()
            try:
                with anyio.fail_after(10):
                    while True:
                        message = await receive()
                        if message['type'] == 'http.disconnect':
                            return
                        body.extend(message.get('body', b''))
                        if len(body) > 65536:
                            await PlainTextResponse('MCP request too large', status_code=413)(scope, receive, send)
                            return
                        if not message.get('more_body'):
                            break
            except TimeoutError:
                await PlainTextResponse('MCP request body timeout', status_code=408)(scope, receive, send)
                return
            pending = True

            async def replay():
                nonlocal pending
                if pending:
                    pending = False
                    return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
                return await receive()
            child_receive = replay
        else:
            child_receive = receive

        async def private_send(message):
            if message['type'] == 'http.response.start':
                message = {**message, 'headers': [*message.get('headers', []),
                                                (b'cache-control', b'private, no-store'),
                                                (b'vary', b'Authorization')]}
            await send(message)

        await child(scope, child_receive, private_send)

    async def metrics(self, scope, receive, send):
        peer = (scope.get('client') or ('',))[0]
        try:
            local = ipaddress.ip_address(peer).is_loopback
        except ValueError:
            local = False
        headers = dict(scope.get('headers', ()))
        if not self.hub.config.test_upstream or not local or b'origin' in headers:
            await PlainTextResponse('Not found', status_code=404)(scope, receive, send)
            return
        from backend.plugin.mcphub.service.auth_service import authenticate_key

        authorization = headers.get(b'authorization', b'').decode('latin-1')
        try:
            if not authorization.startswith('Bearer ') or not await plugin_enabled():
                raise ValueError
            await authenticate_key(authorization[7:], 'pypi')
        except Exception:
            await PlainTextResponse('Unauthorized', status_code=401)(scope, receive, send)
            return
        await JSONResponse(self.hub.executor.snapshot(), headers={'Cache-Control': 'no-store'})(scope, receive, send)
