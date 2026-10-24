"""app.db — SQLite 存储层（WAL 模式，线程本地连接）"""
from __future__ import annotations
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS quotes (
  code TEXT NOT NULL,
  name TEXT,
  category TEXT,
  price REAL,
  change_pct REAL,
  amount REAL,          -- 成交额（元）
  nav REAL,             -- T-1 单位净值
  nav_date TEXT,        -- 净值日期
  premium REAL,         -- 溢价率 %
  tag TEXT,             -- premium/discount/normal/unknown
  sgzt TEXT,            -- 申购状态
  ts INTEGER NOT NULL,  -- 快照时间（秒）
  PRIMARY KEY (code, ts)
);
CREATE INDEX IF NOT EXISTS idx_quotes_ts ON quotes(ts);

CREATE TABLE IF NOT EXISTS fund_nav (
  code TEXT PRIMARY KEY,
  nav REAL,
  nav_date TEXT,
  updated_at INTEGER
);

CREATE TABLE IF NOT EXISTS fund_status (
  code TEXT PRIMARY KEY,
  sgzt TEXT,
  shzt TEXT,
  updated_at INTEGER
);

CREATE TABLE IF NOT EXISTS fund_holdings (
  code TEXT NOT NULL,
  stock TEXT NOT NULL,
  secid TEXT,
  name TEXT,
  weight REAL,
  quarter TEXT,
  updated_at INTEGER,
  PRIMARY KEY (code, stock)
);
CREATE TABLE IF NOT EXISTS fund_shares (
  code TEXT NOT NULL,
  date TEXT NOT NULL,        -- 份额对应日期（YYYY-MM-DD）
  shares REAL,               -- 场内份额（份）
  source TEXT,               -- szse / em
  updated_at INTEGER,
  PRIMARY KEY (code, date)
);

CREATE TABLE IF NOT EXISTS fund_limits (
  code TEXT NOT NULL,
  date TEXT NOT NULL,        -- 快照日期（YYYY-MM-DD，按自然日）
  sgzt TEXT,                 -- 申购状态（开放申购/限大额/暂停申购）
  max_amt REAL,              -- 单日累计购买上限（元；NULL=不限）
  source TEXT,               -- em-page
  updated_at INTEGER,
  PRIMARY KEY (code, date)
);

CREATE TABLE IF NOT EXISTS est_error (
  code TEXT NOT NULL,
  date TEXT NOT NULL,        -- 官方净值日期（配对日）
  est_nav REAL,              -- 当日最后一笔盘中估算净值
  official_nav REAL,         -- 官方净值
  err_pct REAL,              -- (est-official)/official*100，正=估算偏高
  est_source TEXT,           -- official/holdings/anchor
  updated_at INTEGER,
  PRIMARY KEY (code, date)
);

CREATE TABLE IF NOT EXISTS beta_model (
  code TEXT NOT NULL,
  group_key TEXT,            -- fund:{code} 或 group:{est_source}
  beta REAL,                 -- 校准系数：calibrated = raw * beta
  n INTEGER,                 -- 样本数
  level TEXT,                -- fund（个体）/ group（组级兜底）
  updated_at INTEGER,
  PRIMARY KEY (code)
);

CREATE TABLE IF NOT EXISTS trade_days (
  date TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS run_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER,
  module TEXT,
  ok INTEGER,
  message TEXT
);
"""


class DB:
    """SQLite 封装：线程本地连接 + WAL，避免多线程写冲突。"""

    def __init__(self, path):
        self.path = str(path)
        self._local = threading.local()
        with self.conn as c:
            c.executescript(SCHEMA)
            self._migrate(c)

    @staticmethod
    def _migrate(c: sqlite3.Connection):
        """为既有表补列（P1 估值引擎引入）。SQLite 的轻量迁移。"""
        cols = {r["name"] for r in c.execute("PRAGMA table_info(quotes)").fetchall()}
        for col, ddl in (("est_nav", "est_nav REAL"),
                         ("est_source", "est_source TEXT"),
                         ("est_premium", "est_premium REAL"),
                         ("shares", "shares REAL"),
                         ("share_chg_pct", "share_chg_pct REAL"),
                         ("max_amt", "max_amt REAL"),
                         ("limit_chg_pct", "limit_chg_pct REAL"),
                         ("est_nav_raw", "est_nav_raw REAL"),
                         ("beta", "beta REAL")):
            if col not in cols:
                c.execute(f"ALTER TABLE quotes ADD COLUMN {ddl}")
                c.execute("INSERT INTO run_log(ts,module,ok,message) VALUES(?,?,?,?)",
                          (int(time.time()), "db", 1, f"migrate: add column quotes.{col}"))

    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = c
        return c

    # ---------- quotes ----------
    def insert_quotes(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        # 兼容旧调用方未传份额/限额/校准字段
        rows = [{**r, "shares": r.get("shares"),
                 "share_chg_pct": r.get("share_chg_pct"),
                 "max_amt": r.get("max_amt"),
                 "limit_chg_pct": r.get("limit_chg_pct"),
                 "est_nav_raw": r.get("est_nav_raw"),
                 "beta": r.get("beta")} for r in rows]
        sql = """INSERT OR REPLACE INTO quotes
                 (code,name,category,price,change_pct,amount,nav,nav_date,premium,tag,sgzt,ts,
                  est_nav,est_source,est_premium,shares,share_chg_pct,max_amt,limit_chg_pct,
                  est_nav_raw,beta)
                 VALUES (:code,:name,:category,:price,:change_pct,:amount,:nav,:nav_date,:premium,:tag,:sgzt,:ts,
                         :est_nav,:est_source,:est_premium,:shares,:share_chg_pct,:max_amt,:limit_chg_pct,
                         :est_nav_raw,:beta)"""
        with self.conn as c:
            c.executemany(sql, rows)
        return len(rows)

    def latest_quotes(self) -> list[dict]:
        sql = """SELECT q.* FROM quotes q
                 JOIN (SELECT code, MAX(ts) AS mx FROM quotes GROUP BY code) m
                 ON q.code = m.code AND q.ts = m.mx"""
        return [dict(r) for r in self.conn.execute(sql).fetchall()]

    def quote_history(self, code: str, limit: int = 60) -> list[dict]:
        sql = """SELECT * FROM quotes WHERE code = ?
                 ORDER BY ts DESC LIMIT ?"""
        return [dict(r) for r in self.conn.execute(sql, (code, limit)).fetchall()]

    def quote_history_range(self, code: str, days: int = 1,
                            max_pts: int = 400) -> dict:
        """区间历史（多日回看用）：按 days 取数并按粒度降采样。

        days=1 → 5 分钟级；days=7 → 小时级；days>=30 → 天级（末日末值）。
        返回 {points: [{ts, price, premium, est_premium, amount}], stats, data_days}
        stats 基于区间内全部原始点计算（非降采样点），保证统计准确。
        """
        import time as _t
        now = int(_t.time())
        start = now - days * 86400
        rows = self.conn.execute(
            """SELECT ts, price, premium, est_premium, amount FROM quotes
               WHERE code = ? AND ts >= ? ORDER BY ts ASC""",
            (code, start)).fetchall()
        if not rows:
            return {"points": [], "stats": None, "data_days": 0}
        # 数据实际覆盖天数（按北京自然日）
        span_days = len({_t.strftime("%Y-%m-%d", _t.localtime(r["ts"]))
                         for r in rows})
        # 统计：溢价取 est_premium 优先、premium 兜底
        def prem(r):
            return r["est_premium"] if r["est_premium"] is not None else r["premium"]
        ps = [prem(r) for r in rows if prem(r) is not None]
        prices = [r["price"] for r in rows if r["price"] is not None]
        last = rows[-1]
        stats = {
            "n": len(rows),
            "prem_max": max(ps) if ps else None,
            "prem_min": min(ps) if ps else None,
            "prem_avg": sum(ps) / len(ps) if ps else None,
            "prem_last": prem(last),
            "price_max": max(prices) if prices else None,
            "price_min": min(prices) if prices else None,
            "first_ts": rows[0]["ts"], "last_ts": last["ts"],
        }
        # 降采样：按 bucket 取每桶最后一点
        bucket = 300 if days <= 1 else (3600 if days <= 7 else 86400)
        agg: dict[int, dict] = {}
        for r in rows:
            b = r["ts"] // bucket
            agg[b] = {"ts": r["ts"], "price": r["price"],
                      "premium": r["premium"], "est_premium": r["est_premium"],
                      "amount": r["amount"]}
        pts = list(agg.values())
        if len(pts) > max_pts:  # 再按步长抽稀
            step = len(pts) / max_pts
            pts = [pts[int(i * step)] for i in range(max_pts)]
        return {"points": pts, "stats": stats, "data_days": span_days}

    def latest_ts(self) -> int:
        row = self.conn.execute("SELECT MAX(ts) AS mx FROM quotes").fetchone()
        return int(row["mx"]) if row and row["mx"] else 0

    def known_codes(self) -> list[str]:
        rows = self.conn.execute("SELECT DISTINCT code FROM quotes").fetchall()
        return [r["code"] for r in rows]

    # ---------- nav ----------
    def upsert_nav(self, code: str, nav: float, nav_date: str):
        with self.conn as c:
            c.execute("""INSERT INTO fund_nav(code,nav,nav_date,updated_at)
                         VALUES(?,?,?,?)
                         ON CONFLICT(code) DO UPDATE SET
                           nav=excluded.nav, nav_date=excluded.nav_date,
                           updated_at=excluded.updated_at""",
                      (code, nav, nav_date, int(time.time())))

    def nav_map(self) -> dict[str, dict]:
        rows = self.conn.execute("SELECT * FROM fund_nav").fetchall()
        return {r["code"]: dict(r) for r in rows}

    # ---------- status ----------
    def upsert_status(self, code: str, sgzt: str, shzt: str):
        with self.conn as c:
            c.execute("""INSERT INTO fund_status(code,sgzt,shzt,updated_at)
                         VALUES(?,?,?,?)
                         ON CONFLICT(code) DO UPDATE SET
                           sgzt=excluded.sgzt, shzt=excluded.shzt,
                           updated_at=excluded.updated_at""",
                      (code, sgzt, shzt, int(time.time())))

    def status_map(self) -> dict[str, dict]:
        rows = self.conn.execute("SELECT * FROM fund_status").fetchall()
        return {r["code"]: dict(r) for r in rows}

    # ---------- shares（场内份额监控） ----------
    def upsert_shares(self, code: str, date: str, shares: float, source: str = "szse"):
        with self.conn as c:
            c.execute("""INSERT INTO fund_shares(code,date,shares,source,updated_at)
                         VALUES(?,?,?,?,?)
                         ON CONFLICT(code,date) DO UPDATE SET
                           shares=excluded.shares, source=excluded.source,
                           updated_at=excluded.updated_at""",
                      (code, date, shares, source, int(time.time())))

    def share_series(self, code: str, limit: int = 30) -> list[dict]:
        """单只基金份额历史（新→旧）。"""
        rows = self.conn.execute(
            "SELECT * FROM fund_shares WHERE code=? ORDER BY date DESC LIMIT ?",
            (code, limit)).fetchall()
        return [dict(r) for r in rows]

    def share_map(self) -> dict[str, dict]:
        """每只基金最新 + 上一交易日份额，附日变动百分比。

        份额日更但只在交易日变动，故"上一有记录日"就是上一有效观测，
        无需交易日历；阅览端按 shares_chg_pct 排序即可找异动。
        """
        sql = """SELECT s.code, s.date, s.shares,
                        (SELECT p.shares FROM fund_shares p
                          WHERE p.code = s.code AND p.date < s.date
                          ORDER BY p.date DESC LIMIT 1) AS prev_shares
                 FROM fund_shares s
                 JOIN (SELECT code, MAX(date) mx FROM fund_shares GROUP BY code) m
                   ON s.code = m.code AND s.date = m.mx"""
        out = {}
        for r in self.conn.execute(sql).fetchall():
            cur, prev = r["shares"], r["prev_shares"]
            chg = None
            if cur is not None and prev not in (None, 0):
                chg = (cur - prev) / prev * 100.0
            out[r["code"]] = {"code": r["code"], "date": r["date"],
                              "shares": cur, "prev_shares": prev,
                              "chg_pct": chg}
        return out

    # ---------- limits（申购限额） ----------
    def upsert_limits(self, rows: list[dict]):
        """写入当日申购限额快照（按自然日去重，重复抓取同日覆盖）。"""
        if not rows:
            return
        today = time.strftime("%Y-%m-%d")
        now = int(time.time())
        with self.conn as c:
            c.executemany(
                """INSERT INTO fund_limits(code,date,sgzt,max_amt,source,updated_at)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(code,date) DO UPDATE SET
                     sgzt=excluded.sgzt, max_amt=excluded.max_amt,
                     source=excluded.source, updated_at=excluded.updated_at""",
                [(r["code"], today, r.get("sgzt"), r.get("max_amt"),
                  "em-page", now) for r in rows])

    def limit_map(self) -> dict[str, dict]:
        """每只基金最新 + 上一有记录日的限额，附变化（限额突涨/突降信号）。

        限额变化不按交易日（公告可任意日发布），故与份额同理取"上一有记录日"对比。
        """
        sql = """SELECT s.code, s.date, s.sgzt, s.max_amt,
                        (SELECT p.max_amt FROM fund_limits p
                          WHERE p.code = s.code AND p.date < s.date
                          ORDER BY p.date DESC LIMIT 1) AS prev_max_amt,
                        (SELECT p.sgzt FROM fund_limits p
                          WHERE p.code = s.code AND p.date < s.date
                          ORDER BY p.date DESC LIMIT 1) AS prev_sgzt
                 FROM fund_limits s
                 JOIN (SELECT code, MAX(date) mx FROM fund_limits GROUP BY code) m
                   ON s.code = m.code AND s.date = m.mx"""
        out = {}
        for r in self.conn.execute(sql).fetchall():
            cur, prev = r["max_amt"], r["prev_max_amt"]
            chg = None
            if cur is not None and prev not in (None, 0):
                chg = (cur - prev) / prev * 100.0
            out[r["code"]] = {"code": r["code"], "date": r["date"],
                              "sgzt": r["sgzt"], "max_amt": cur,
                              "prev_max_amt": prev, "prev_sgzt": r["prev_sgzt"],
                              "chg_pct": chg}
        return out

    # ---------- calibration（估算误差 + β 校准） ----------
    def last_est_snapshot(self, code: str, start: int, end: int) -> dict | None:
        """某自然日窗口内最后一笔带估算净值的快照（校准配对用）。"""
        row = self.conn.execute(
            """SELECT est_nav, est_source, ts FROM quotes
               WHERE code=? AND ts BETWEEN ? AND ? AND est_nav IS NOT NULL
               ORDER BY ts DESC LIMIT 1""", (code, start, end)).fetchone()
        return dict(row) if row else None

    def record_est_errors(self, rows: list[dict]) -> int:
        """写入估算误差（按 基金+净值日期 去重，重复跑不叠加）。"""
        if not rows:
            return 0
        now = int(time.time())
        with self.conn as c:
            cur = c.executemany(
                """INSERT OR IGNORE INTO est_error
                   (code,date,est_nav,official_nav,err_pct,est_source,updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                [(r["code"], r["date"], r["est_nav"], r["official_nav"],
                  r["err_pct"], r.get("est_source"), now) for r in rows])
            return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    def est_error_all(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM est_error ORDER BY date").fetchall()]

    def est_error_recent(self, code: str, limit: int = 5) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM est_error WHERE code=? ORDER BY date DESC LIMIT ?",
            (code, limit)).fetchall()]

    def upsert_betas(self, rows: list[dict]):
        """重建 β 模型表（全量替换）。"""
        if not rows:
            return
        now = int(time.time())
        with self.conn as c:
            c.execute("DELETE FROM beta_model")
            c.executemany(
                """INSERT INTO beta_model(code,group_key,beta,n,level,updated_at)
                   VALUES(?,?,?,?,?,?)""",
                [(r["code"], r["group_key"], r["beta"], r["n"], r["level"], now)
                 for r in rows])

    def beta_map(self) -> dict[str, dict]:
        rows = self.conn.execute("SELECT * FROM beta_model").fetchall()
        return {r["code"]: dict(r) for r in rows}

    # ---------- holdings ----------
    def replace_holdings(self, code: str, rows: list[dict], quarter: str = "latest"):
        now = int(time.time())
        with self.conn as c:
            c.execute("DELETE FROM fund_holdings WHERE code=?", (code,))
            c.executemany(
                """INSERT INTO fund_holdings(code,stock,secid,name,weight,quarter,updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                [(code, r["stock"], r.get("secid"), r.get("name"),
                  r.get("weight"), quarter or "latest", now) for r in rows])

    def holdings_map(self, code: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM fund_holdings WHERE code=? ORDER BY weight DESC", (code,)
        ).fetchall()
        return [dict(r) for r in rows]

    def holdings_updated_at(self, code: str):
        row = self.conn.execute(
            "SELECT MAX(updated_at) AS mx FROM fund_holdings WHERE code=?", (code,)
        ).fetchone()
        return int(row["mx"]) if row and row["mx"] else 0

    # ---------- trade days ----------
    def set_trade_days(self, dates: set[str]):
        with self.conn as c:
            c.execute("DELETE FROM trade_days")
            c.executemany("INSERT INTO trade_days(date) VALUES(?)",
                          [(d,) for d in dates])

    def trade_day_set(self) -> set[str]:
        return {r["date"] for r in self.conn.execute("SELECT date FROM trade_days").fetchall()}

    # ---------- log ----------
    def log(self, module: str, ok: bool, message: str):
        with self.conn as c:
            c.execute("INSERT INTO run_log(ts,module,ok,message) VALUES(?,?,?,?)",
                      (int(time.time()), module, 1 if ok else 0, message[:500]))
