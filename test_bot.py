import json
import os
import sys
import unittest
from decimal import Decimal
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
import bot

M = '0x' + '1'*64
A = '0x' + '2'*40
C = 12345

class MemoryRedis:
    def __init__(self):
        self.data = {}
        self.sets = {}

    def __call__(self, op, *args):
        op = op.upper()
        key = args[0]
        if op == 'GET':
            return self.data.get(key)
        if op == 'SET':
            if len(args)>2 and args[2] == 'NX' and key in self.data:
                return None
            self.data[key] = str(args[1]); return 'OK'
        if op == 'DEL':
            return int(self.data.pop(key, None) is not None)
        if op == 'SADD':
            target = self.sets.setdefault(key, set())
            old = len(target); target.add(args[1]); return len(target)-old
        if op == 'SREM':
            target = self.sets.setdefault(key, set())
            old = len(target); target.discard(args[1]); return old-len(target)
        if op == 'SMEMBERS':
            return list(self.sets.get(key, set()))
        if op == 'SCARD':
            return len(self.sets.get(key, set()))
        raise AssertionError(op)

class TestBot(unittest.TestCase):
    def setUp(self):
        self.redis = MemoryRedis()
        self.p1 = patch.object(bot, 'redis', self.redis)
        self.p2 = patch.object(bot, 'ALERTS_ENABLED', True)
        self.p1.start(); self.p2.start()
        self.addCleanup(self.p1.stop); self.addCleanup(self.p2.stop)

    def message(self, s, chat=C):
        return {'text':s, 'chat':{'id':chat,'type':'private'}}

    def test_start(self):
        self.assertIn('/watch',bot.reply(self.message('/start')))

    def test_demo_is_labeled(self):
        v = bot.reply(self.message('/demoalert'))
        self.assertIn('SIMULATED',v)
        self.assertIn('NOT A REAL POSITION',v)

    def test_watch_persist_and_unwatch(self):
        with patch.object(bot,'estimate_health',return_value=Decimal('1.40')):
            msg = bot.reply(self.message(f'/watch {M} {A}'))
        self.assertIn('Watch saved',msg)
        key=bot.watch_id(C,M,A)
        self.assertIn(key,bot.reply(self.message('/watches')))
        self.assertEqual(bot.reply(self.message(f'/unwatch {key}',chat=444)), 'Watch not found in your chat.')
        self.assertIn('removed',bot.reply(self.message(f'/unwatch {key}')))
        self.assertIn('no active watches',bot.reply(self.message('/watches')))

    def test_watch_no_debt(self):
        with patch.object(bot,'estimate_health',return_value=None):
            answer=bot.reply(self.message(f'/watch {M} {A}'))
        self.assertIn('not created',answer)
        self.assertEqual(self.redis('SCARD',bot.GLOBAL_SET),0)

    def test_alarm_transition_no_duplicates(self):
        with patch.object(bot,'estimate_health',return_value=Decimal('1.40')):
            bot.reply(self.message(f'/watch {M} {A}'))
        alerts=[]
        with patch.object(bot,'send_telegram',side_effect=lambda cid,msg:alerts.append((cid,msg))):
            with patch.object(bot,'estimate_health',return_value=Decimal('1.15')):
                bot.run_monitor();bot.run_monitor()
            self.assertEqual(len(alerts),1)
            with patch.object(bot,'estimate_health',return_value=Decimal('1.04')):
                bot.run_monitor()
        self.assertEqual(len(alerts),2)
        self.assertIn('Critical risk',alerts[-1][1])

    def test_invalid_threshold_and_private_chat(self):
        self.assertIn('between',bot.reply(self.message(f'/watch {M} {A} NaN')))
        message=self.message(f'/watch {M} {A}')
        message['chat']['type']='group'
        self.assertIn('private chat',bot.reply(message))

    def test_estimate_math(self):
        def mocked_get(path,allow_404=False):
            if path.endswith('/position'):
                return {'borrow_shares':'1000000','collateral_assets':str(2*10**18)}
            if path.endswith('/state') and '/oracles/' not in path:
                return {'total_borrow_assets':str(100*10**18),'total_borrow_shares':'1000000'}
            if path.startswith('/v0/oracles'):
                return {'price':str(100*10**36)}
            return {'oracle_address':A,'lltv_wad':str(8*10**17)}
        with patch.object(bot,'market_get',side_effect=mocked_get):
            hf=bot.estimate_health(M,A)
        self.assertGreater(hf,Decimal('1'))

if __name__=='__main__':
    unittest.main()