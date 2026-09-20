import unittest
from unittest.mock import patch

import server
from simulation_clock import clock
from world import World, place_center


class ButtonTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = clock.time()
        self.world = World()
        self.patch = patch.object(server, 'world', self.world)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        clock.now = self.now

    async def test_work_explains_rejection_and_pays_only_after_shift(self):
        w = self.world
        result = await server.handle({'type': 'work'})
        self.assertIn('Go to', result)
        self.assertIsNone(w.shift)
        w.you.x, w.you.y = place_center('farm')
        before = w.you.cash
        self.assertIn('set to work', await server.handle({'type': 'work'}))
        self.assertEqual(w.you.cash, before)
        self.assertIn('already', await server.handle({'type': 'work'}))
        clock.advance(6.8)
        self.assertEqual(w.snapshot()['player']['shift'], 1)
        clock.advance(0.2)
        w.finish_shift()
        self.assertEqual(w.you.cash, before + 3)
        w.finish_shift()
        self.assertEqual(w.you.cash, before + 3)
        w.phase = 'night'
        self.assertIn('asleep', await server.handle({'type': 'work'}))

    async def test_whisper_is_recorded_and_affects_named_shop(self):
        w = self.world
        price = w.shops[0].share_price
        result = await server.handle({'type': 'whisper', 'agent': 'kit', 'text': 'Mira sells rotten bread.'})
        self.assertIn('Rumour shared with Kit', result)
        self.assertLess(w.shops[0].share_price, price)
        self.assertIn('Mira sells rotten bread.', w.agents['kit'].memories[-1].text)
        count = len(w.agents['kit'].memories)
        self.assertIn('Type a rumour', await server.handle({'type': 'whisper', 'agent': 'kit', 'text': '  '}))
        self.assertEqual(len(w.agents['kit'].memories), count)

    async def test_crash_moves_goods_and_shares_and_shortage_removes_stock(self):
        w = self.world
        goods = [g for _, g in w.all_goods()]
        for g in goods:
            g['price'] = g['base'] = 20
            g['qty'] = 6
        price = w.shops[0].share_price
        self.assertIn('Market crash', await server.handle({'type': 'event', 'name': 'crash'}))
        self.assertTrue(all(g['price'] == 12 for g in goods))
        self.assertLess(w.shops[0].share_price, price)
        self.assertIn('units lost', await server.handle({'type': 'event', 'name': 'shortage'}))
        self.assertTrue(all(g['qty'] == 3 and g['price'] > 12 for g in goods))
        w.policy = 'cheap_bread'
        server.god_event('shortage')
        self.assertTrue(all(g['price'] <= 4 for g in goods if g['name'].lower() in ('bread', 'apples')))

    async def test_festival_changes_cash_energy_and_destinations(self):
        w = self.world
        before = {a.id: a.cash for a in [*w.agents.values(), w.you]}
        self.assertIn('Festival', await server.handle({'type': 'event', 'name': 'festival'}))
        for a in [*w.agents.values(), w.you]:
            self.assertEqual(a.cash, before[a.id] + 15)
            self.assertEqual(a.energy, 100)
        positions = {(a.tx, a.ty) for a in w.agents.values()}
        self.assertEqual(len(positions), len(w.agents))
        self.assertTrue(all(14 < x < 20 and 8 < y < 12 for x, y in positions))

    async def test_stranger_is_a_real_agent_and_election_opens(self):
        w = self.world
        count = len(w.agents)
        self.assertIn('Rowan arrived', await server.handle({'type': 'event', 'name': 'stranger'}))
        self.assertEqual(len(w.agents), count + 1)
        self.assertEqual(w.agents['rowan'].cash, 200)
        self.assertIn('rowan', [a['id'] for a in w.snapshot()['agents']])
        self.assertIn('already in town', await server.handle({'type': 'event', 'name': 'stranger'}))
        self.assertEqual(len(w.agents), count + 1)
        self.assertIn('Election opened', await server.handle({'type': 'event', 'name': 'election'}))
        self.assertTrue(w.ballot_open)
        self.assertIn('already open', await server.handle({'type': 'event', 'name': 'election'}))


if __name__ == '__main__':
    unittest.main()
