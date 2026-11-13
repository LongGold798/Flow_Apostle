import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from engine import EventEngine
from feed import SHANGHAI, stock_code
from rules import Rules


class RulesTest(unittest.TestCase):
    def setUp(self):
        config = json.loads(Path(__file__).with_name("config.json").read_text(encoding="utf-8"))
        self.alerts = []
        self.rules = Rules(config, self.alerts.append)
        self.start = datetime(2026, 11, 13, 9, 31, tzinfo=SHANGHAI)

    def send(self, topic, seconds, key, **row):
        getattr(self.rules, topic)((self.start + timedelta(seconds=seconds), {key: dict(name="测试", **row)}))

    def test_price_window_cooldown_and_gap(self):
        for t, p in [(0, 10), (10, 10), (16, 10.2), (21, 10.3), (120, 9)]:
            self.send("stocks", t, "600000", price=p, volume=None, amount=None)
        self.assertEqual([x["kind"] for x in self.alerts], ["快速上涨"])

    def test_volume_and_amount_have_independent_baselines(self):
        for t in range(0, 113, 8):
            self.send("stocks", t, "600000", price=10, volume=t * 100, amount=t * 100000)
        self.send("stocks", 120, "600000", price=10, volume=22000, amount=22000000)
        self.assertEqual({x["kind"] for x in self.alerts}, {"成交量放大", "成交额放大"})

    def test_limit_state_sequence(self):
        for t, state, breaks in [(0, "封涨停", 0), (15, "炸板", 1), (30, "封涨停", 1),
                                 (45, "炸板", 2), (60, "封涨停", 2), (75, "封涨停", 2)]:
            self.send("limits", t, "600000", state=state, breaks=breaks, seal=None)
        self.assertEqual([x["kind"] for x in self.alerts], ["炸板", "回封", "炸板", "回封"])

    def test_board_price_breadth_and_money(self):
        key = "行业", "测试板块"
        self.send("board_prices", 0, key, pct=0.2, up=5, down=10)
        self.send("board_prices", 60, key, pct=1.2, up=11, down=4)
        self.send("board_money", 0, key, net=1_000_000)
        self.send("board_money", 60, key, net=-35_000_000)
        self.assertEqual([x["kind"] for x in self.alerts],
                         ["板块快速上涨", "板块上涨联动", "板块资金净流出增加"])

    def test_market_scope(self):
        self.assertEqual(stock_code(1), "000001")
        self.assertEqual(stock_code("688001"), "688001")
        self.assertIsNone(stock_code("920001"))
        self.assertIsNone(stock_code("200001"))

    def test_event_dispatch(self):
        engine = EventEngine()
        engine.subscribe("stock", lambda data: engine.publish("alert", data))
        engine.subscribe("alert", self.alerts.append)
        engine.publish("stock", "消息")
        engine.drain()
        self.assertEqual(self.alerts, ["消息"])


if __name__ == "__main__":
    unittest.main()
