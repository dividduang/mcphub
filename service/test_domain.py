"""Real SQL regression tests. Run only against an explicitly approved disposable FBA database.

MCPHUB_TEST_ALLOW_DB=1 uv run python -m unittest backend.plugin.mcphub.service.test_domain
All fixture writes are enclosed in an outer transaction and rolled back.
"""

import os
import unittest

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from backend.app.admin.model.user import User
from backend.common.exception import errors
from backend.database.db import async_engine
from backend.plugin.mcphub.crud.hub import effective_tools
from backend.plugin.mcphub.model.entities import McpGrant, McpKey, McpServer, McpTool, new_id
from backend.plugin.mcphub.schema.hub import CreateKey, CreateRequest, UpdateKey
from backend.plugin.mcphub.service import hub_service
from backend.plugin.mcphub.service.auth_service import secret_digest


@unittest.skipUnless(os.environ.get('MCPHUB_TEST_ALLOW_DB') == '1', 'Explicit disposable DB approval required')
class DelegationRegression(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine(async_engine.url, poolclass=NullPool)
        self.connection = await self.engine.connect()
        self.transaction = await self.connection.begin()
        self.db = AsyncSession(bind=self.connection, expire_on_commit=False)
        self.users = []
        for label in ('D', 'F'):
            user = User(username=f'mcphub_test_{label}_{new_id()}', nickname=label, password=None, salt=None)
            self.db.add(user)
            self.users.append(user)
        server = McpServer(slug=f'test-{new_id()}', name='Regression')
        self.db.add(server)
        await self.db.flush()
        self.tools = [McpTool(server_id=server.id, name=f'tool_{index}', title=str(index)) for index in range(1, 7)]
        self.db.add_all(self.tools)
        await self.db.flush()
        self.d, self.f = self.users
        self.scope = [self.tools[index].id for index in (0, 2, 4)]

    async def asyncTearDown(self):
        await self.db.close()
        await self.transaction.rollback()
        await self.connection.close()
        await self.engine.dispose()

    async def key(self, ids):
        return await hub_service.create_key(self.db, self.d.id, CreateKey(name='test', tool_ids=ids))

    async def names(self, key_id, user_id):
        return {tool.name for tool in await effective_tools(self.db, key_id, user_id)}

    async def test_distinct_keys_and_no_implicit_new_tool(self):
        await hub_service.grant_tools(self.db, self.d.id, self.scope, self.d.id)
        k13 = await self.key(self.scope[:2])
        k5 = await self.key(self.scope[2:])
        self.assertEqual(await self.names(k13['key']['id'], self.d.id), {'tool_1', 'tool_3'})
        self.assertEqual(await self.names(k5['key']['id'], self.d.id), {'tool_5'})
        await hub_service.grant_tools(self.db, self.d.id, [self.tools[1].id], self.d.id)
        self.assertEqual(await self.names(k13['key']['id'], self.d.id), {'tool_1', 'tool_3'})
        stored = await self.db.get(McpKey, k13['key']['id'])
        self.assertEqual(stored.secret_hash, secret_digest(k13['secret']))
        self.assertNotEqual(stored.secret_hash, k13['secret'])
        self.assertNotIn('secret', await hub_service.key_data(self.db, stored))
        with self.assertRaises(errors.NotFoundError):
            await hub_service.revoke_key(self.db, self.f.id, stored.id)
        with self.assertRaises(errors.ForbiddenError):
            await hub_service.update_key(self.db, self.d.id, stored.id, UpdateKey(tool_ids=self.scope))

    async def test_regrant_does_not_revive_previous_binding(self):
        await hub_service.grant_tools(self.db, self.d.id, self.scope[:1], self.d.id)
        issued = await self.key(self.scope[:1])
        grant = await self.db.scalar(select(McpGrant).where(McpGrant.user_id == self.d.id, McpGrant.tool_id == self.scope[0]))
        old_generation = grant.generation
        await hub_service.revoke_grant(self.db, grant.id)
        self.assertEqual(await self.names(issued['key']['id'], self.d.id), set())
        await hub_service.grant_tools(self.db, self.d.id, self.scope[:1], self.d.id)
        self.assertNotEqual(grant.generation, old_generation)
        self.assertEqual(await self.names(issued['key']['id'], self.d.id), set())
        replacement = await self.key(self.scope[:1])
        self.assertEqual(await self.names(replacement['key']['id'], self.d.id), {'tool_1'})

    async def test_pending_policy_and_decision_boundaries(self):
        pending = await hub_service.submit_request(self.db, self.d.id, CreateRequest(tool_ids=self.scope))
        self.assertEqual((pending['status'], pending['granted_tool_ids']), ('pending', []))
        with self.assertRaises(errors.ForbiddenError):
            await self.key(self.scope)
        with self.assertRaises(errors.RequestError):
            await hub_service.decide_request(self.db, pending['id'], [self.tools[1].id], self.f.id)
        decided = await hub_service.decide_request(self.db, pending['id'], self.scope[:1], self.f.id)
        self.assertEqual((decided['status'], decided['granted_tool_ids']), ('partial', self.scope[:1]))
        with self.assertRaises(errors.ConflictError):
            await hub_service.decide_request(self.db, pending['id'], self.scope, self.f.id)
        self.tools[1].auto_approve = True
        await self.db.flush()
        policy = await hub_service.submit_request(self.db, self.d.id, CreateRequest(tool_ids=[self.tools[1].id, self.tools[3].id]))
        self.assertEqual((policy['status'], policy['granted_tool_ids']), ('partial', [self.tools[1].id]))
        final = await hub_service.decide_request(self.db, policy['id'], [], self.f.id)
        self.assertEqual(final['granted_tool_ids'], [self.tools[1].id])

    async def test_shrink_disable_revoke_and_server_gate(self):
        await hub_service.grant_tools(self.db, self.d.id, self.scope, self.d.id)
        issued = await self.key(self.scope)
        key_id = issued['key']['id']
        reduced = await hub_service.update_key(self.db, self.d.id, key_id, UpdateKey(tool_ids=self.scope[:1]))
        self.assertEqual(reduced['effective_tool_ids'], self.scope[:1])
        disabled = await hub_service.update_key(self.db, self.d.id, key_id, UpdateKey(enabled=False))
        self.assertEqual(disabled['effective_tool_ids'], [])
        enabled = await hub_service.update_key(self.db, self.d.id, key_id, UpdateKey(enabled=True))
        self.assertEqual(enabled['effective_tool_ids'], self.scope[:1])
        self.tools[0].enabled = False
        await self.db.flush()
        self.assertEqual(await self.names(key_id, self.d.id), set())
        await hub_service.revoke_key(self.db, self.d.id, key_id)
        with self.assertRaises(errors.ConflictError):
            await hub_service.update_key(self.db, self.d.id, key_id, UpdateKey(enabled=True))
