import argparse
from functools import partial
import json
import sqlite3
from pathlib import Path
from threading import Event, Thread
import tkinter as tk
from tkinter import ttk

from engine import EventEngine
from feed import AkShareFeed, now, poll, session
from rules import Rules

ROOT = Path(__file__).resolve().parent
LABELS = dict(stocks="沪深行情", board_prices="板块行情", board_money="板块资金", limits="涨跌停池")


def calendar(ak):
    dates = set(ak.tool_trade_date_hist_sina()["trade_date"])
    if not dates or max(dates) < now().date():
        raise ValueError("交易日历未覆盖今天")
    return dates


def table(parent, fields):
    frame = ttk.Frame(parent)
    frame.pack(fill="both", expand=True)
    keys = [key for key, _, _ in fields]
    grid = ttk.Treeview(frame, columns=keys, show="headings")
    scrollbar = ttk.Scrollbar(frame, orient="vertical", command=grid.yview)
    grid.configure(yscrollcommand=scrollbar.set)
    scrollbar.pack(side="right", fill="y")
    grid.pack(side="left", fill="both", expand=True)
    for key, label, width in fields:
        grid.heading(key, text=label)
        grid.column(key, width=width, minwidth=50, stretch=key == keys[-1])
    return grid


def display(value, digits=2):
    return "—" if value is None else f"{value:,.{digits}f}"


class Monitor:
    def __init__(self, root, ak, config):
        self.root = root
        self.config = config
        self.engine = EventEngine()
        self.stop = Event()
        self.states = {}
        self.boards = {}
        self.money = {}
        self.day = None
        self.dates = None
        self.root.title("Flow Apostle · A 股异动监控")
        self.root.geometry("1240x750")
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        data = ROOT / "data"
        data.mkdir(exist_ok=True)
        self.db = sqlite3.connect(data / "events.sqlite3")
        self.db.execute("CREATE TABLE IF NOT EXISTS alerts (time TEXT NOT NULL, payload TEXT NOT NULL)")
        self.db.execute("CREATE INDEX IF NOT EXISTS alerts_time ON alerts(time)")
        self.status = tk.StringVar(value="正在加载交易日历…")
        ttk.Label(root, textvariable=self.status, padding=12, justify="left").pack(fill="x")
        notebook = ttk.Notebook(root)
        notebook.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        alerts = ttk.Frame(notebook, padding=8)
        ranks = ttk.Frame(notebook, padding=8)
        notebook.add(alerts, text="当日异动")
        notebook.add(ranks, text="板块资金榜")
        toolbar = ttk.Frame(alerts)
        toolbar.pack(fill="x", pady=(0, 8))
        ttk.Label(toolbar, text="异动类型").pack(side="left", padx=(0, 8))
        self.filter = ttk.Combobox(toolbar, state="readonly", values=["全部"])
        self.filter.set("全部")
        self.filter.pack(side="left")
        self.filter.bind("<<ComboboxSelected>>", lambda _: self.refresh_alerts())
        self.alert_grid = table(alerts, [
            ("time", "时间", 84), ("category", "类别", 70), ("code", "代码 / 板块", 118),
            ("name", "名称", 115), ("kind", "异动", 145), ("price", "价格", 65),
            ("pct", "日涨跌 %", 80), ("window", "窗口 s", 65), ("move", "短周期变化", 85),
            ("reason", "触发原因", 370),
        ])
        self.detail = tk.StringVar()
        ttk.Label(alerts, textvariable=self.detail, wraplength=1120).pack(fill="x", pady=(8, 0))
        self.alert_grid.bind("<<TreeviewSelect>>", self.show_detail)
        controls = ttk.Frame(ranks)
        controls.pack(fill="x", pady=(0, 8))
        self.category = ttk.Combobox(controls, values=["行业", "概念"], state="readonly", width=8)
        self.order = ttk.Combobox(controls, values=["净流入", "净流出"], state="readonly", width=9)
        for combo, value in ((self.category, "行业"), (self.order, "净流入")):
            combo.set(value)
            combo.pack(side="left", padx=(0, 8))
            combo.bind("<<ComboboxSelected>>", lambda _: self.refresh_ranks())
        ttk.Label(controls, text="东方财富 · 今日主力净流入统计").pack(side="left")
        self.rank_grid = table(ranks, [
            ("rank", "排名", 70), ("name", "板块", 190), ("net", "净额（万元）", 140),
            ("pct", "涨跌幅 %", 120), ("up", "上涨家数", 110), ("down", "下跌家数", 110),
        ])

        rules = Rules(config, self.save_alert)
        for topic in LABELS:
            self.engine.subscribe(topic, getattr(rules, topic))
        self.engine.subscribe("status", self.update_status)
        self.engine.subscribe("board_prices", self.update_boards)
        self.engine.subscribe("board_money", self.update_money)
        self.engine.subscribe("calendar", self.set_calendar)
        Thread(target=self.start_feeds, args=(ak,), daemon=True).start()
        self.tick()

    def start_feeds(self, ak):
        while not self.stop.is_set():
            try:
                dates = calendar(ak)
                break
            except Exception as exc:
                self.engine.publish("calendar", str(exc))
                self.stop.wait(30)
        if self.stop.is_set():
            return
        self.engine.publish("calendar", dates)
        feed = AkShareFeed(ak)
        for topic in LABELS:
            args = (topic, partial(feed.fetch, topic), self.config["poll_seconds"][topic],
                    self.config["max_fetch_seconds"], dates, self.engine.publish, self.stop)
            Thread(target=poll, args=args, daemon=True).start()

    def set_calendar(self, result):
        self.dates = result

    def update_status(self, data):
        topic, received, elapsed, count, changed, error = data
        old = self.states.get(topic)
        updated = received if changed else old[5] if old else None
        self.states[topic] = received, elapsed, count, changed, error, updated

    def update_boards(self, data):
        self.boards = data[1]
        self.refresh_ranks()

    def update_money(self, data):
        self.money = data[1]
        self.refresh_ranks()

    def refresh_ranks(self):
        self.rank_grid.delete(*self.rank_grid.get_children())
        positive = self.order.get() == "净流入"
        selected = [(key, row["net"]) for key, row in self.money.items()
                    if key[0] == self.category.get() and (row["net"] > 0 if positive else row["net"] < 0)]
        for rank, (key, value) in enumerate(sorted(selected, key=lambda item: item[1], reverse=positive), 1):
            board = self.boards.get(key, {})
            self.rank_grid.insert("", "end", values=(rank, key[1], display(value / 10000),
                                  display(board.get("pct")), display(board.get("up"), 0), display(board.get("down"), 0)))

    def save_alert(self, row):
        self.db.execute("INSERT INTO alerts VALUES (?, ?)", (row["time"], json.dumps(row, ensure_ascii=False)))
        self.dirty = True

    def refresh_alerts(self):
        date = now().date().isoformat()
        kinds = set(self.filter["values"]) | {"全部"}
        where = "WHERE time >= ? AND time < ?"
        params = [date, date + "T24"]
        if self.filter.get() != "全部":
            where += " AND json_extract(payload, '$.kind') = ?"
            params.append(self.filter.get())
        recent = self.db.execute("SELECT payload FROM alerts " + where + " ORDER BY time DESC, rowid DESC LIMIT 800", params)
        self.alert_grid.delete(*self.alert_grid.get_children())
        for (payload,) in recent:
            row = json.loads(payload)
            kinds.add(row["kind"])
            self.alert_grid.insert("", "end", values=(row["time"][11:19], row["category"], row["code"], row["name"],
                                   row["kind"], display(row["price"]), display(row["pct"]), row["window"] or "—",
                                   display(row["move"]), row["reason"]))
        self.filter["values"] = ["全部", *sorted(kinds - {"全部"})]

    def show_detail(self, _):
        selection = self.alert_grid.selection()
        if selection:
            self.detail.set(self.alert_grid.item(selection[0], "values")[-1])

    def tick(self):
        self.dirty = False
        self.engine.drain()
        at = now()
        if self.dirty:
            self.db.commit()
        if self.day != at.date():
            self.day = at.date()
            self.money.clear()
            self.boards.clear()
            self.refresh_ranks()
            self.dirty = True
        if self.dirty:
            self.refresh_alerts()
        if isinstance(self.dates, set):
            active = at.date() in self.dates and session(at)
            lines = [f"{at:%Y-%m-%d %H:%M:%S} 北京时间 · " +
                     ("盘中轮询 · 时间为收到快照的本地时间" if active else "休市 · 停止轮询")]
            for topic, label in LABELS.items():
                state = self.states.get(topic)
                if state:
                    received, elapsed, count, _, error, changed = state
                    age = (at - changed).total_seconds() if changed else float("inf")
                    stale = age > max(self.config["max_sample_gap_seconds"], self.config["poll_seconds"][topic] * 2)
                    text = error or f"{count} 条 · 耗时 {elapsed:.1f}s" + (" · 快照未更新" if stale else "")
                    lines.append(f"{label}  {received:%H:%M:%S}  {text[:140]}")
                else:
                    lines.append(f"{label}  等待首批数据")
            self.status.set("\n".join(lines))
        elif self.dates:
            self.status.set(f"交易日历获取失败，将重试 · {self.dates[:140]}")
        self.root.after(500, self.tick)

    def close(self):
        self.stop.set()
        self.db.close()
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description="Flow Apostle A 股异动监控")
    parser.add_argument("--probe", action="store_true", help="直接请求各真实接口，可在休市时运行")
    args = parser.parse_args()
    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    import akshare as ak

    if args.probe:
        feed = AkShareFeed(ak)
        failed = False
        for topic, fetch in [("calendar", lambda _: calendar(ak)),
                             *((name, getattr(feed, name)) for name in LABELS)]:
            try:
                print(f"{topic}: OK ({len(fetch(now().date()))} rows)", flush=True)
            except Exception as exc:
                failed = True
                print(f"{topic}: {type(exc).__name__}: {exc}", flush=True)
        return int(failed)

    root = tk.Tk()
    ttk.Style(root).theme_use("clam")
    Monitor(root, ak, config)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
