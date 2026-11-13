from collections import defaultdict, deque
from statistics import median

from feed import session


class Rules:
    def __init__(self, config, emit):
        self.config = config
        self.emit = emit
        self.history = defaultdict(deque)
        self.alerted = {}
        self.limit_states = {}
        self.limit_day = None
        self.limit_sample = None
        self.windows = sorted((int(k), v) for k, v in config["windows"].items())

    def alert(self, at, key, row, kind, reason, window=0, move=None, transition=False):
        if kind in self.config["disabled_events"]:
            return
        alert_key = key, kind, window
        previous = self.alerted.get(alert_key)
        if not transition and previous and (at - previous).total_seconds() < self.config["cooldown_seconds"]:
            return
        self.alerted[alert_key] = at
        category, code = key if isinstance(key, tuple) else ("个股", key)
        self.emit(dict(time=at.isoformat(timespec="seconds"), code=code, category=category, name=row["name"],
                       kind=kind, price=row.get("price"), pct=row.get("pct"), window=window, move=move,
                       reason=reason, industry=row.get("industry")))

    def samples(self, key, at, row, horizon):
        samples = self.history[key]
        if samples:
            last = samples[-1][0]
            if at <= last:
                return None
            if session(last) != session(at) or (at - last).total_seconds() > self.config["max_sample_gap_seconds"]:
                samples.clear()
        samples.append((at, row))
        while len(samples) > 1 and (at - samples[1][0]).total_seconds() > horizon:
            samples.popleft()
        return samples

    @staticmethod
    def prior(samples, at, window):
        for timestamp, row in reversed(samples):
            age = (at - timestamp).total_seconds()
            if age >= window:
                return (age, row) if age <= window * 1.5 else (None, None)
        return None, None

    def stocks(self, data):
        at, rows = data
        intervals = self.config["volume_baseline_intervals"]
        for code, row in rows.items():
            samples = self.samples(code, at, row, max(self.windows[-1][0] * 1.5, 60 * intervals))
            if not samples:
                continue
            for window, threshold in self.windows:
                age, old = self.prior(samples, at, window)
                if old is None:
                    continue
                move = (row["price"] / old["price"] - 1) * 100
                if abs(move) >= threshold:
                    kind = "快速上涨" if move > 0 else "快速下跌"
                    self.alert(at, code, row, kind, f"{age:.0f} 秒内涨跌 {move:+.2f}%，阈值 {threshold}%", window, move)
            self.activity(at, code, row, samples, intervals)

    def activity(self, at, code, row, samples, intervals):
        window = self.windows[0][0]
        age, old = self.prior(samples, at, window)
        if old is None:
            return
        amount = row["amount"]
        if amount is None or old["amount"] is None or amount - old["amount"] < self.config["min_window_amount"]:
            return
        history = [(t, r) for t, r in samples if (at - t).total_seconds() >= age]
        history = history[-(intervals + 1):]
        if len(history) < intervals + 1:
            return
        for metric, kind, unit in (("volume", "成交量放大", "手"), ("amount", "成交额放大", "元")):
            if row[metric] is None or old[metric] is None:
                continue
            rates = []
            for (t0, r0), (t1, r1) in zip(history, history[1:]):
                if r0[metric] is None or r1[metric] is None or r1[metric] < r0[metric]:
                    break
                rates.append((r1[metric] - r0[metric]) / (t1 - t0).total_seconds())
            if len(rates) != intervals:
                continue
            baseline = median(rates)
            delta = row[metric] - old[metric]
            if baseline > 0 and delta / age >= baseline * self.config["volume_ratio"]:
                move = (row["price"] / old["price"] - 1) * 100
                self.alert(at, code, row, kind,
                           f"{age:.0f} 秒累计 {delta:,.0f} {unit}，为该股此前时段成交速率中位数的 {delta / age / baseline:.1f} 倍",
                           window, move)

    def board_prices(self, data):
        at, rows = data
        window = self.config["board_window_seconds"]
        for key, row in rows.items():
            samples = self.samples(("prices", key), at, row, window * 1.5)
            if not samples:
                continue
            age, old = self.prior(samples, at, window)
            if old is None:
                continue
            if row["pct"] is not None and old["pct"] is not None:
                move = row["pct"] - old["pct"]
                if abs(move) >= self.config["board_change_points"]:
                    self.alert(at, key, row, "板块快速上涨" if move > 0 else "板块快速下跌",
                               f"{age:.0f} 秒内板块涨跌幅变化 {move:+.2f} 个百分点", window, move)
            for field, other, kind in (("up", "down", "板块上涨联动"), ("down", "up", "板块下跌联动")):
                if any(r[k] is None for r in (row, old) for k in (field, other)):
                    continue
                delta = row[field] - old[field]
                if delta >= self.config["board_breadth_count"] and row[other] <= old[other]:
                    self.alert(at, key, row, kind, f"{age:.0f} 秒内对应方向家数净增 {delta:.0f} 家", window)

    def board_money(self, data):
        at, rows = data
        window = self.config["board_money_window_seconds"]
        for key, row in rows.items():
            samples = self.samples(("money", key), at, row, window * 1.5)
            if not samples:
                continue
            age, old = self.prior(samples, at, window)
            if old is None:
                continue
            delta = row["net"] - old["net"]
            if abs(delta) >= self.config["board_money_change_yuan"]:
                kind = "板块资金净流入增加" if delta > 0 else "板块资金净流出增加"
                self.alert(at, key, row, kind,
                           f"{age:.0f} 秒内主力累计净额变化 {delta:+,.0f} 元，当前 {row['net']:+,.0f} 元", window)

    def limits(self, data):
        at, rows = data
        if at.date() != self.limit_day:
            self.limit_day = at.date()
            self.limit_states.clear()
            self.limit_sample = None
        previous_sample = self.limit_sample
        self.limit_sample = at
        continuous = (previous_sample is not None and session(previous_sample) == session(at)
                      and (at - previous_sample).total_seconds() <= self.config["max_sample_gap_seconds"])
        for code, row in rows.items():
            state = row["state"]
            previous = self.limit_states.get(code)
            self.limit_states[code] = row
            kind = None
            if continuous and previous is None:
                kind = {"封涨停": "观测到封涨停", "封跌停": "观测到封跌停", "炸板": "观测到炸板"}[state]
            elif continuous and previous["state"] != state:
                kind = "回封" if previous["state"] == "炸板" and state == "封涨停" else {
                    "封涨停": "观测到封涨停", "封跌停": "观测到封跌停", "炸板": "炸板"}[state]
            elif continuous and state == "封涨停" and previous["breaks"] is not None and row["breaks"] is not None:
                if row["breaks"] > previous["breaks"]:
                    kind = "炸板后回封"
            if kind:
                reason = f"东方财富盘中池状态 {state}"
                if row["breaks"] is not None:
                    reason += f"，炸板次数 {row['breaks']:.0f}"
                if row["seal"] is not None:
                    reason += f"，封板资金 {row['seal']:,.0f} 元"
                if row.get("industry"):
                    reason += f"，行业 {row['industry']}"
                self.alert(at, code, row, kind, reason, transition=True)
        # A missing pool entry alone cannot confirm an opening or a feed failure.
