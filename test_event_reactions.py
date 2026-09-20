import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

import server
from simulation_clock import clock
from world import World


class EventReactionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = clock.time()
        self.world = World()
        self.world_patch = patch.object(server, 'world', self.world)
        self.tasks_patch = patch.object(server, 'agent_tasks', {})
        self.world_patch.start()
        self.tasks_patch.start()

    def tearDown(self):
        self.tasks_patch.stop()
        self.world_patch.stop()
        clock.now = self.now

    async def test_events_have_personal_reactions_and_priority_in_prompts(self):
        for kind in ('crash', 'festival', 'shortage', 'election', 'stranger'):
            server.god_event(kind)
            says = set()
            for ag in self.world.agents.values():
                self.assertEqual(ag.event_reaction['kind'], kind)
                self.assertEqual(ag.event_reaction['turns'], 3)
                self.assertLess(ag.next_tick - clock.time(), 3)
                self.assertTrue(server.build_prompt(ag).startswith('PRIORITY: React to'))
                self.assertIn(ag.goal, server.build_prompt(ag))
                says.add(ag.say)
            self.assertEqual(len(says), len(self.world.agents))

    async def test_invalid_model_actions_get_a_useful_reaction(self):
        server.god_event('crash')
        ag = self.world.agents['bram']
        invalid = {'thought': 'Do something', 'action': 'source', 'target': '', 'arg': '',
                   'say': 'I will protect the bank.'}
        with patch.object(server.llm, 'chat', AsyncMock(return_value=json.dumps(invalid))):
            await server._agent_tick(ag)
        self.assertEqual(ag.last_action, 'walked to the bank')
        self.assertTrue(any('attempted event response failed' in m.text for m in ag.memories))
        invalid.update(action='gossip', target='bram', arg='I am gossiping to myself.')
        with patch.object(server.llm, 'chat', AsyncMock(return_value=json.dumps(invalid))):
            await server._agent_tick(ag)
        self.assertEqual(ag.last_action, 'walked to the bank')

    async def test_api_failure_still_produces_relevant_actions(self):
        server.god_event('shortage')
        fig = self.world.agents['fig']
        crop = fig.inventory.get(fig.produces, 0)
        with patch.object(server.llm, 'chat', AsyncMock(return_value='')):
            await server._agent_tick(fig)
            await server._agent_tick(self.world.agents['devi'])
        self.assertEqual(fig.inventory[fig.produces], crop + 6)
        self.assertEqual(self.world.agents['devi'].promise, 'workers_stipend')
        self.assertTrue(any(e['text'].startswith('Fig on the shortage') for e in self.world.log))

    async def test_followthrough_lasts_three_turns_and_interrupts_sleep(self):
        ag = self.world.agents['mira']
        server.god_event('crash')
        reply = json.dumps({'thought': 'Protect the shop.', 'action': 'move_to', 'target': 'shop',
                            'say': 'The crash has cut my margins. Time to find customers.'})
        with patch.object(self.world, 'asleep', return_value=True), \
             patch.object(server.llm, 'chat', AsyncMock(return_value=reply)) as chat:
            for _ in range(3):
                await server._agent_tick(ag)
            self.assertEqual(chat.await_count, 3)
            self.assertIsNone(ag.event_reaction)
            await server._agent_tick(ag)
            self.assertEqual(chat.await_count, 3)
            self.assertEqual(ag.last_action, 'slept')
        self.assertEqual(sum(e['text'].startswith('Mira on the market crash') for e in self.world.log), 1)

    async def test_new_event_cancels_pending_turn(self):
        ag = self.world.agents['mira']
        started = asyncio.Event()

        async def delayed(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()

        with patch.object(server.llm, 'chat', delayed):
            task = asyncio.create_task(server._agent_tick(ag))
            server.agent_tasks[ag.id] = task
            await started.wait()
            server.god_event('festival')
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(ag.event_reaction['kind'], 'festival')
        self.assertIn('celebrating', ag.say)

    async def test_late_uncancellable_result_cannot_overwrite_newer_event(self):
        ag = self.world.agents['mira']
        started, release = asyncio.Event(), asyncio.Event()

        async def delayed(*args, **kwargs):
            started.set()
            await release.wait()
            return '{"thought":"Old plan","action":"rest","say":"Ignore the news."}'

        with patch.object(server.llm, 'chat', delayed):
            task = asyncio.create_task(server._agent_tick(ag))
            await started.wait()
            server.god_event('crash')
            expected = ag.say
            release.set()
            await task
        self.assertEqual(ag.say, expected)
        self.assertEqual(ag.event_reaction['turns'], 3)

    async def test_festival_keeps_dance_positions_during_reactions(self):
        server.god_event('festival')
        ag = self.world.agents['kit']
        destination = ag.tx, ag.ty
        with patch.object(server.llm, 'chat', AsyncMock(return_value='')):
            await server._agent_tick(ag)
        self.assertEqual((ag.tx, ag.ty), destination)
        server.god_event('crash')
        self.assertEqual(ag.event_reaction['kind'], 'crash')
        self.assertIn('the market crash', server.build_prompt(ag))


if __name__ == '__main__':
    unittest.main()
