import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

import llm
import server
from simulation_clock import clock
from world import World


class PauseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = clock.time()
        self.world = World()
        self.patches = [patch.object(server, 'world', self.world),
                        patch.object(server, 'agent_tasks', {}),
                        patch.object(server, 'chat_tasks', set()),
                        patch.object(llm, 'paused', False)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        clock.now = self.now

    async def test_pause_cancels_active_and_queued_requests_including_player_chat(self):
        entered = asyncio.Event()

        async def stalled(*args):
            entered.set()
            await asyncio.Event().wait()

        with patch.object(llm, 'API_KEY', 'test'), patch.object(llm, '_gate', asyncio.Semaphore(1)), \
             patch.object(llm, '_once', AsyncMock(side_effect=stalled)) as request:
            for ag in list(self.world.agents.values())[:3]:
                server.agent_tasks[ag.id] = asyncio.create_task(server._agent_tick(ag))
            await server.handle({'type': 'chat', 'agent': 'kit', 'text': 'Hello'})
            await entered.wait()
            tasks = [*server.agent_tasks.values(), *server.chat_tasks]
            await server.handle({'type': 'pause', 'paused': True})
            await asyncio.gather(*tasks, return_exceptions=True)
            self.assertTrue(all(task.cancelled() for task in tasks))
            self.assertEqual(request.await_count, 1)
            self.assertEqual(await llm.chat('system', 'user'), '')
            await server._agent_tick(self.world.agents['mira'])
            await server.player_chat(self.world.agents['kit'], 'Hello again')
            self.assertIn('paused', await server.handle({'type': 'chat', 'agent': 'kit', 'text': 'More'}))
            self.assertEqual(request.await_count, 1)
            server.set_paused(False)
            request.side_effect = None
            request.return_value = json.dumps({'action': 'rest', 'say': 'Back again.'})
            await server._agent_tick(self.world.agents['mira'])
            self.assertEqual(request.await_count, 2)

    async def test_untracked_semaphore_waiter_cannot_start_after_pause(self):
        gate = asyncio.Semaphore(0)
        with patch.object(llm, 'API_KEY', 'test'), patch.object(llm, '_gate', gate), \
             patch.object(llm, '_once', AsyncMock()) as request:
            task = asyncio.create_task(llm.chat('system', 'user'))
            await asyncio.sleep(0)
            server.set_paused(True)
            gate.release()
            self.assertEqual(await task, '')
            request.assert_not_awaited()

    async def test_simulation_freezes_without_catching_up_on_resume(self):
        self.world.set_speed(10)
        server.set_paused(True)
        with patch.object(server, 'broadcast', AsyncMock()), \
             patch.object(server, 'agent_tick', AsyncMock()) as think:
            task = asyncio.create_task(server.sim_loop())
            try:
                await asyncio.sleep(.2)
                self.assertEqual(clock.time(), self.now)
                think.assert_not_awaited()
                server.set_paused(False)
                await asyncio.sleep(.12)
                self.assertGreater(clock.time(), self.now)
                self.assertLess(clock.time() - self.now, 2)
                self.assertEqual(self.world.speed, 10)
            finally:
                task.cancel()
                await asyncio.gather(task, *server.agent_tasks.values(), return_exceptions=True)

    async def test_paused_actions_and_festival_timer_are_frozen(self):
        server.god_event('festival')
        server.set_paused(True)
        before = self.world.snapshot()
        await asyncio.sleep(.03)
        for msg in ({'type': 'event', 'name': 'crash'}, {'type': 'work'},
                    {'type': 'move', 'x': 1, 'y': 1}, {'type': 'speed', 'speed': 20}):
            self.assertIn('paused', await server.handle(msg))
        self.assertEqual(self.world.snapshot(), before)
        server.set_paused(False)
        remaining = self.world.snapshot()['festival']['remaining']
        self.assertAlmostEqual(remaining, before['festival']['remaining'], delta=.02)

    async def test_late_response_is_ignored_even_after_resume(self):
        started, release = asyncio.Event(), asyncio.Event()
        ag = self.world.agents['mira']

        async def delayed(*args, **kwargs):
            started.set()
            await release.wait()
            return '{"action":"work","thought":"Stale decision","say":"Too late"}'

        with patch.object(llm, 'chat', delayed):
            task = asyncio.create_task(server._agent_tick(ag))
            await started.wait()
            server.set_paused(True)
            server.set_paused(False)
            release.set()
            await task
        self.assertNotEqual(ag.thought, 'Stale decision')
        self.assertNotEqual(ag.say, 'Too late')


if __name__ == '__main__':
    unittest.main()
