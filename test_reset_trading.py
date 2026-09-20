import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

import server
from simulation_clock import clock
from world import World, SHARES


class ResetTradingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = clock.time()
        self.world = World()
        self.patches = [patch.object(server, 'world', self.world),
                        patch.object(server, 'agent_tasks', {}),
                        patch.object(server, 'chat_tasks', set()),
                        patch.object(server.llm, 'paused', False)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        clock.now = self.now

    async def test_reset_clears_progress_and_preserves_pause_without_loading_catalog(self):
        w = self.world
        w.you.cash = 2
        w.day = 7
        w.set_speed(20)
        w.shops[0].goods[0]['qty'] = 0
        server.god_event('stranger')
        server.god_event('festival')
        server.set_paused(True)
        with patch('world.catalog.load_stock', side_effect=AssertionError('Reset must not fetch stock')):
            result = await server.handle({'type': 'reset'})
        fresh = server.world
        self.assertIn('Still paused', result)
        self.assertNotEqual(fresh.town_id, w.town_id)
        self.assertEqual((fresh.day, fresh.speed, fresh.you.cash), (1, 1, 100))
        self.assertTrue(fresh.paused and server.llm.paused)
        self.assertEqual(fresh.shops[0].goods[0]['qty'], 6)
        self.assertNotIn('rowan', fresh.agents)
        self.assertIsNone(fresh.speedup_summary)
        self.assertEqual(fresh.conversations, [])
        self.assertEqual(fresh.festival_id, 0)
        self.assertEqual(len(fresh.shops[0].goods), len(w.shops[0].goods))
        server.reset_town()
        self.assertEqual(len(server.world.shops[0].goods), len(fresh.shops[0].goods))

    async def test_reset_cancels_tasks_and_discards_late_old_world_decisions(self):
        started, release = asyncio.Event(), asyncio.Event()
        ag = self.world.agents['bram']

        async def delayed(*args, **kwargs):
            started.set()
            await release.wait()
            return '{"action":"buy_shares","target":"mira","arg":"2","say":"Old trade"}'

        with patch.object(server.llm, 'chat', delayed):
            late = asyncio.create_task(server._agent_tick(ag))
            await started.wait()
            cancellable = asyncio.create_task(asyncio.sleep(60))
            server.agent_tasks['mira'] = cancellable
            server.reset_town()
            release.set()
            await late
            await asyncio.gather(cancellable, return_exceptions=True)
        self.assertTrue(cancellable.cancelled())
        self.assertEqual(server.agent_tasks, {})
        self.assertEqual(server.world.shops[0].holders.get('bram', 0), 0)
        self.assertEqual(server.world.agents['bram'].cash, 300)

    async def test_normal_agent_turn_can_buy_and_sell_with_conserved_balances(self):
        w = self.world
        ag, sh = w.agents['bram'], w.shops[0]
        initial_cash = ag.cash + w.exchange['cash']
        initial_quote = w.quote(sh)
        old_cash = ag.cash
        replies = [json.dumps({'action': action, 'target': 'mira', 'arg': '2', 'say': action})
                   for action in ('buy_shares', 'sell_shares')]
        with patch.object(server.llm, 'chat', AsyncMock(side_effect=replies)) as chat:
            await server._agent_tick(ag)
            self.assertEqual(sh.holders[ag.id], 2)
            self.assertEqual(ag.cash, old_cash - 2 * initial_quote)
            await server._agent_tick(ag)
            self.assertEqual(chat.await_count, 2)
        self.assertEqual(sh.holders[ag.id], 0)
        self.assertEqual(ag.cash + w.exchange['cash'], initial_cash)
        self.assertEqual(sum(sh.holders.values()) + w.float_of(sh), SHARES)
        self.assertEqual(ag.decisions, 2)
        self.assertTrue(any('Bram bought 2 shares' in e['text'] for e in w.log))
        self.assertTrue(any('Bram sold 2 shares' in e['text'] for e in w.log))

    async def test_prompt_shows_holdings_and_sell_option_with_no_shares_on_offer(self):
        w = self.world
        ag, sh = w.agents['bram'], w.shops[0]
        w.trade_shares(ag, sh, w.float_of(sh), True)
        self.assertFalse(w.market_open())
        ag.decisions = 3
        prompt = server.build_prompt(ag)
        self.assertIn('PORTFOLIO REVIEW', prompt)
        self.assertIn('available=0', prompt)
        self.assertIn('you own=5', prompt)
        self.assertIn('target=mira', prompt)
        self.assertIn('sell_shares', prompt)
        ag.decisions = 1
        self.assertNotIn('PORTFOLIO REVIEW', server.build_prompt(ag))

    async def test_portfolio_turn_buys_and_sells_without_api_calls(self):
        w = self.world
        ag, sh = w.agents['bram'], w.shops[0]
        ag.trust[sh.owner] = .3
        ag.decisions = 3
        with patch.object(server.llm, 'chat', AsyncMock()) as chat:
            await server._agent_tick(ag)
            self.assertGreater(sh.holders.get(ag.id, 0), 0)
            self.assertLessEqual(sh.holders[ag.id], 3)
            self.assertGreaterEqual(ag.cash, 12)
            held = sh.holders[ag.id]
            ag.cash = 0
            ag.decisions = 7
            await server._agent_tick(ag)
            self.assertLess(sh.holders[ag.id], held)
            self.assertGreater(ag.cash, 0)
            chat.assert_not_awaited()
        self.assertEqual(sum(sh.holders.values()) + w.float_of(sh), SHARES)

    async def test_portfolio_respects_debts_pause_and_event_priority(self):
        w = self.world
        ag = w.agents['bram']
        ag.debts = {'mira': 1000}
        self.assertIsNone(w.stock_decision(ag))
        ag.debts.clear()
        ag.decisions = 3
        server.set_paused(True)
        with patch.object(server.llm, 'chat', AsyncMock()) as chat:
            await server._agent_tick(ag)
            chat.assert_not_awaited()
        self.assertEqual(ag.decisions, 3)
        self.assertEqual(w.shops[0].holders.get(ag.id, 0), 0)
        server.set_paused(False)
        server.god_event('festival')
        reply = json.dumps({'action': 'rest', 'say': 'A dance before I check the books.'})
        with patch.object(server.llm, 'chat', AsyncMock(return_value=reply)) as chat:
            await server._agent_tick(ag)
            chat.assert_awaited_once()
        self.assertEqual(w.shops[0].holders.get(ag.id, 0), 0)

    async def test_trade_quantities_and_holdings_are_validated(self):
        w = self.world
        ag, sh = w.agents['bram'], w.shops[0]
        for quantity in ('-1', '0', '1.5', 'oops'):
            before = (ag.cash, w.float_of(sh))
            result = w.apply_action(ag, 'buy_shares', 'mira', quantity, '')
            self.assertIn('positive whole number', result)
            self.assertEqual((ag.cash, w.float_of(sh)), before)
        self.assertIn('bought 2 shares', w.apply_action(ag, 'buy_shares', '', 'mira, 2', ''))
        self.assertIn('sold 2 shares', w.apply_action(ag, 'sell_shares', 'mira', '20', ''))
        self.assertEqual(sh.holders[ag.id], 0)
        self.assertIn('holds no shares', w.apply_action(ag, 'sell_shares', 'mira', '1', ''))


if __name__ == '__main__':
    unittest.main()
