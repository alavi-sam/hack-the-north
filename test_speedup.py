import asyncio
import unittest
from unittest.mock import Mock, patch

import server
from simulation_clock import clock
from world import DAY_SECONDS, World


class SpeedupTests(unittest.TestCase):
    def setUp(self):
        self.start = clock.time()
        self.world = World()

    def tearDown(self):
        clock.now = self.start

    def test_recap_only_covers_active_period_and_major_purchases(self):
        w = self.world
        w.event('Before fast-forward', 'Purchase', 50)
        w.set_speed(5)
        w.event('Small purchase', 'Purchase', 19)
        w.event('Large purchase', 'Purchase', 20)
        for _ in range(150):
            w.event('Ordinary news')
        w.set_speed(20)
        clock.advance(60)
        w.shops[0].share_price *= 1.2
        w.set_speed(1)
        recap = w.snapshot()['speedup_summary']
        self.assertEqual([e['text'] for e in recap['events']], ['Large purchase'])
        self.assertEqual(recap['hours'], round(60 / DAY_SECONDS * 24, 1))
        self.assertEqual(recap['markets'][0]['percent'], 20)
        w.set_speed(1)
        self.assertEqual(w.speedup_summary, recap)
        w.set_speed(10)
        self.assertIsNone(w.speedup_summary)
        w.set_speed(1)
        self.assertEqual(w.speedup_summary['events'], [])
        self.assertEqual(w.speedup_summary['markets'], [])

    def test_fast_time_reaches_election_and_records_result(self):
        w = self.world
        w.set_speed(20)
        for _ in range(int(DAY_SECONDS * 5 * 4)):
            clock.advance(0.25)
            w.step(0.25)
        w.set_speed(1)
        self.assertEqual(w.day, 6)
        self.assertTrue(w.mayor)
        self.assertTrue(any(e['category'] == 'Election'
                            for e in w.speedup_summary['events']))

    def test_invalid_speeds_do_not_change_state(self):
        for value in (0, -1, 100, '20', None, True, 5.0):
            self.world.set_speed(value)
        self.assertEqual(self.world.speed, 1)
        self.assertIsNone(self.world.speedup)


class LoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_loop_scales_time_without_overlapping_agent_turns(self):
        start = clock.time()
        w = World()
        w.set_speed(20)
        calls = []
        finished = asyncio.Event()

        async def slow_turn(ag):
            calls.append(ag.id)
            await finished.wait()

        async def no_broadcast(msg):
            pass

        # Each iteration represents one real second, hence 20 simulated seconds.
        with patch.object(server, 'world', w), \
             patch.object(server, 'time', Mock(monotonic=Mock(side_effect=range(10000)))), \
             patch.object(server, 'agent_tick', slow_turn), \
             patch.object(server, 'broadcast', no_broadcast):
            task = asyncio.create_task(server.sim_loop())
            try:
                await asyncio.sleep(0.3)
                self.assertGreaterEqual(clock.time() - start, 60)
                self.assertEqual(len(calls), len(w.agents))
                self.assertEqual(len(set(calls)), len(calls))
            finally:
                task.cancel()
                finished.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                await asyncio.sleep(0)
                clock.now = start


if __name__ == '__main__':
    unittest.main()
