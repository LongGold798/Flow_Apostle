from math import isfinite
from threading import Lock
from importlib import import_module
from time import monotonic
from datetime import datetime
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


def now():
    return datetime.now(SHANGHAI)


def session(at):
    clock = at.hour * 60 + at.minute
    if at.weekday() < 5:
        if 570 <= clock < 690:
            return at.date(), "上午"
        if 780 <= clock < 900:
            return at.date(), "下午"
    return None


def number(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if isfinite(result) else None


def stock_code(value):
    code = str(value).zfill(6)
    if len(code) == 6 and code.isascii() and code.isdigit() and code.startswith(("60", "68", "00", "30")):
        return code
    return None


def records(frame, required):
    missing = set(required) - set(frame.columns)
    if missing:
        raise ValueError("接口字段缺失 " + ", ".join(sorted(missing)))
    return frame.to_dict("records")


class AkShareFeed:
    def __init__(self, ak):
        self.ak = ak
        self.push2_lock = Lock()
        self.push2_retry_at = 0

    def fetch(self, topic, day):
        if topic == "limits":
            return self.limits(day)
        if monotonic() < self.push2_retry_at:
            raise RuntimeError("东方财富限流冷却中")
        if not self.push2_lock.acquire(blocking=False):
            raise RuntimeError("行情接口正在处理其他请求")
        try:
            if monotonic() < self.push2_retry_at:
                raise RuntimeError("东方财富限流冷却中")
            return getattr(self, topic)(day)
        except Exception as exc:
            if type(exc).__name__ == "Push2RateLimitError":
                self.push2_retry_at = monotonic() + 300
            raise
        finally:
            self.push2_lock.release()

    def stocks(self, day):
        raw = records(self.ak.stock_zh_a_spot_em(), ["代码", "名称", "最新价", "涨跌幅", "成交量", "成交额"])
        rows = {}
        for row in raw:
            code = stock_code(row["代码"])
            price = number(row["最新价"])
            if code and price is not None and price > 0:
                rows[code] = dict(name=row["名称"], price=price, pct=number(row["涨跌幅"]),
                                  volume=number(row["成交量"]), amount=number(row["成交额"]))
        if len(rows) < 4000:
            raise ValueError(f"沪深有效行情仅 {len(rows)} 条，可能是分页截断或接口异常")
        return rows

    def board_prices(self, day):
        rows = {}
        for category, fetch in (("行业", self.ak.stock_board_industry_name_em),
                                ("概念", self.ak.stock_board_concept_name_em)):
            # AKShare 1.20.38 caches the internal board list indefinitely.
            module = import_module(fetch.__module__)
            getattr(module, "__" + fetch.__name__).cache_clear()
            raw = records(fetch(), ["板块名称", "涨跌幅", "上涨家数", "下跌家数"])
            if not raw:
                raise ValueError(f"{category}板块行情为空")
            for row in raw:
                name = str(row["板块名称"])
                rows[(category, name)] = dict(name=name, pct=number(row["涨跌幅"]),
                                             up=number(row["上涨家数"]), down=number(row["下跌家数"]))
        return rows

    def board_money(self, day):
        rows = {}
        for category in ("行业", "概念"):
            raw = records(self.ak.stock_sector_fund_flow_rank(indicator="今日", sector_type=category + "资金流"),
                          ["名称", "今日主力净流入-净额"])
            if not raw:
                raise ValueError(f"{category}资金榜为空")
            for row in raw:
                name = str(row["名称"])
                amount = number(row["今日主力净流入-净额"])
                if amount is not None:
                    rows[(category, name)] = dict(name=name, net=amount)
        if not rows:
            raise ValueError("板块资金金额全部缺失")
        return rows

    def limits(self, day):
        rows = {}
        for state, fetch in (("炸板", self.ak.stock_zt_pool_zbgc_em),
                             ("封跌停", self.ak.stock_zt_pool_dtgc_em),
                             ("封涨停", self.ak.stock_zt_pool_em)):
            frame = fetch(date=day.strftime("%Y%m%d"))
            if frame.empty:
                continue
            for row in records(frame, ["代码", "名称", "最新价", "涨跌幅"]):
                code = stock_code(row["代码"])
                if code:
                    rows[code] = dict(name=row["名称"], state=state, price=number(row["最新价"]),
                                      pct=number(row["涨跌幅"]), breaks=number(row.get("炸板次数")),
                                      seal=number(row.get("封板资金", row.get("封单资金"))),
                                      industry=row.get("所属行业"), amount=number(row.get("成交额")))
        return rows


def poll(topic, fetch, interval, max_fetch, calendar, publish, stop):
    previous = None
    failures = 0
    while not stop.is_set():
        start = now()
        current = session(start) if start.date() in calendar else None
        if current is None:
            stop.wait(3)
            continue
        started = monotonic()
        try:
            rows = fetch(start.date())
            received = now()
            elapsed = monotonic() - started
            if session(received) != current or elapsed > max_fetch:
                raise ValueError(f"请求耗时 {elapsed:.1f} 秒或跨交易时段")
            failures = 0
            changed = previous != (current, rows)
            previous = current, rows
            publish("status", (topic, received, elapsed, len(rows), changed, ""))
            if changed or topic == "limits":
                publish(topic, (received, rows))
        except Exception as exc:
            failures += 1
            publish("status", (topic, now(), monotonic() - started, 0, False, f"{type(exc).__name__}: {exc}"))
        delay = min(120, interval * 2 ** min(failures, 4)) if failures else interval
        stop.wait(max(1, delay - (monotonic() - started)))
