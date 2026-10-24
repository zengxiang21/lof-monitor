"""scripts.store — JSONL 增量存储（GitHub Actions 云端采集的数据源）

设计目标：电脑关机时由 GitHub Actions 每 5 分钟采集，数据必须
- 跨 run 持久（不依赖缓存/数据库）
- git 增量友好（追加式，delta 压缩高效）
- 人可读、可审计

布局（相对 data/store/）：
- YYYY-MM-DD.jsonl   每 tick 一行：{"ts":..,"items":[[code,price,chg,amount,
                     premium,est_nav,est_premium],...]}（约 16KB/行）
- state.json         慢变量日更状态：name/category/nav/nav_date/sgzt/shzt/
                     shares/share_chg_pct/max_amt/limit_chg_pct/est_source/beta
- shares.jsonl       每日一行份额快照（算份额日变动用）
- limits.jsonl       每日一行限额快照（算限额变动用）
- errors.jsonl       每日一行估算误差（β 校准用）

本机（有 SQLite）继续走 app/db.py；云端走本模块，两路导出同一套 JSON。
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

TZ_CN = timezone(timedelta(hours=8))

# tick 行 items 的字段位次
F_CODE, F_PRICE, F_CHG, F_AMOUNT, F_PREM, F_EST_NAV, F_EST_PREM = range(7)


def cn_now() -> datetime:
    return datetime.now(TZ_CN)


def cn_date(ts: float | None = None) -> str:
    return (datetime.fromtimestamp(ts, TZ_CN) if ts else cn_now()).strftime("%Y-%m-%d")


class Store:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._state: dict | None = None

    # ---------- 通用文件 ----------

    def _day_path(self, date: str) -> Path:
        return self.root / f"{date}.jsonl"

    def _dates_desc(self, limit_days: int) -> list[str]:
        """最近 limit_days 个自然日（新→旧），只返回存在的文件。"""
        out = []
        d = cn_now().date()
        for _ in range(limit_days + 1):
            p = self._day_path(d.isoformat())
            if p.exists():
                out.append(d.isoformat())
            d -= timedelta(days=1)
        return out

    @staticmethod
    def _last_line(path: Path) -> str:
        """高效读取文件最后一行（只读尾部 64KB）。"""
        try:
            size = path.stat().st_size
            with path.open("rb") as f:
                if size <= 65536:
                    f.seek(0)
                else:
                    f.seek(-65536, 2)
                tail = f.read()
            lines = [ln for ln in tail.decode("utf-8", "ignore").splitlines() if ln.strip()]
            return lines[-1] if lines else ""
        except OSError:
            return ""

    # ---------- tick 写入 / 读取 ----------

    def append_tick(self, rows: list[dict], ts: int | None = None) -> int:
        """追加一个采集 tick。rows 为完整行情行（含 est_* 字段）。返回条数。"""
        ts = ts or int(time.time())
        items = []
        for r in rows:
            items.append([
                str(r.get("code") or "").zfill(6),
                r.get("price"), r.get("change_pct"), r.get("amount"),
                r.get("premium"), r.get("est_nav"), r.get("est_premium"),
            ])
        line = json.dumps({"ts": ts, "items": items},
                          ensure_ascii=False, separators=(",", ":"))
        path = self._day_path(cn_date(ts))
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        return len(items)

    def _read_lines(self, date: str) -> list[dict]:
        path = self._day_path(date)
        if not path.exists():
            return []
        out = []
        with path.open("r", encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln:
                    try:
                        out.append(json.loads(ln))
                    except json.JSONDecodeError:
                        continue
        return out

    def latest_tick(self) -> dict | None:
        """最近一个 tick 行（跨日期回退）。"""
        for date in self._dates_desc(3):
            ln = self._last_line(self._day_path(date))
            if ln:
                try:
                    return json.loads(ln)
                except json.JSONDecodeError:
                    continue
        return None

    def last_tick_on(self, date: str) -> dict | None:
        """指定日期（YYYY-MM-DD）的最后一个 tick 行。"""
        ln = self._last_line(self._day_path(date))
        if ln:
            try:
                return json.loads(ln)
            except json.JSONDecodeError:
                return None
        return None

    def fund_est_on(self, code: str, date: str) -> dict | None:
        """指定日期某基金最后一个快照的 est 字段（误差配对用）。"""
        tick = self.last_tick_on(date)
        if not tick:
            return None
        code = str(code).zfill(6)
        for it in tick.get("items", []):
            if str(it[F_CODE]).zfill(6) == code:
                return {"est_nav": it[F_EST_NAV], "ts": tick.get("ts")}
        return None

    def latest_ts(self) -> int:
        t = self.latest_tick()
        return int(t.get("ts") or 0) if t else 0

    def latest_rows(self) -> list[dict]:
        """最近 tick + state 合并 → 与 db.latest_quotes() 同行形状。"""
        tick = self.latest_tick()
        if not tick:
            return []
        state = self.load_state().get("funds", {})
        rows = []
        for it in tick.get("items", []):
            code = str(it[F_CODE]).zfill(6)
            st = state.get(code, {})
            rows.append({
                "code": code,
                "name": st.get("name"),
                "category": st.get("category") or "A股",
                "price": it[F_PRICE],
                "change_pct": it[F_CHG],
                "amount": it[F_AMOUNT],
                "nav": st.get("nav"),
                "nav_date": st.get("nav_date"),
                "premium": it[F_PREM],
                "tag": self._tag(it[F_PREM]),
                "sgzt": st.get("sgzt"),
                "ts": tick.get("ts"),
                "est_nav": it[F_EST_NAV],
                "est_source": st.get("est_source"),
                "est_premium": it[F_EST_PREM],
                "shares": st.get("shares"),
                "share_chg_pct": st.get("share_chg_pct"),
                "max_amt": st.get("max_amt"),
                "limit_chg_pct": st.get("limit_chg_pct"),
                "est_nav_raw": st.get("est_nav_raw"),
                "beta": st.get("beta", 1.0),
            })
        return rows

    @staticmethod
    def _tag(prem) -> str:
        if prem is None:
            return "unknown"
        if prem >= 2.0:
            return "premium"
        if prem <= -2.0:
            return "discount"
        return "normal"

    def known_codes(self) -> list[str]:
        codes = set(self.load_state().get("funds", {}).keys())
        tick = self.latest_tick()
        if tick:
            codes.update(str(it[F_CODE]).zfill(6) for it in tick.get("items", []))
        return sorted(codes)

    def series(self, code: str, pts: int = 90) -> list[list]:
        """最近 N 个快照点 → [[ts, premium, est_premium], ...]（新→旧）。"""
        code = str(code).zfill(6)
        out: list[list] = []
        for date in self._dates_desc(4):
            for line in reversed(self._read_lines(date)):
                for it in line.get("items", []):
                    if str(it[F_CODE]).zfill(6) == code:
                        out.append([line["ts"], it[F_PREM], it[F_EST_PREM]])
                        break
                if len(out) >= pts:
                    return out
            if len(out) >= pts:
                break
        return out

    def range_points(self, code: str, days: int) -> list[dict]:
        """区间原始点（dict 列表，供 app.ranges.range_from_points 降采样）。

        最近 8 天用全部 tick 行；更早的每日只取最后一行（日级分辨率），
        兼顾精度与读取量。
        """
        code = str(code).zfill(6)
        now = time.time()
        start = now - days * 86400
        fine_days = min(days, 8)
        pts: list[dict] = []
        for date in self._dates_desc(days):
            lines = self._read_lines(date)
            if not lines:
                continue
            if date in self._dates_desc(fine_days - 1):
                use = lines                      # 全量 tick
            else:
                use = [lines[-1]]                # 仅末日
            for line in use:
                if line.get("ts", 0) < start:
                    continue
                for it in line.get("items", []):
                    if str(it[F_CODE]).zfill(6) == code:
                        pts.append({"ts": line["ts"], "price": it[F_PRICE],
                                    "premium": it[F_PREM],
                                    "est_premium": it[F_EST_PREM],
                                    "amount": it[F_AMOUNT]})
                        break
        pts.sort(key=lambda p: p["ts"])
        return pts

    # ---------- state ----------

    def load_state(self) -> dict:
        if self._state is None:
            p = self.root / "state.json"
            try:
                self._state = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
            except (OSError, json.JSONDecodeError):
                self._state = {}
            self._state.setdefault("funds", {})
        return self._state

    def save_state(self) -> None:
        st = self.load_state()
        st["updated_ts"] = int(time.time())
        st["updated"] = cn_date()
        (self.root / "state.json").write_text(
            json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")

    def upsert_fund_state(self, code: str, **fields) -> None:
        funds = self.load_state()["funds"]
        cur = funds.get(str(code).zfill(6), {})
        cur.update({k: v for k, v in fields.items() if v is not None})
        funds[str(code).zfill(6)] = cur

    # ---------- 日更序列（份额/限额/误差） ----------

    def _append_daily(self, name: str, date: str, payload: dict) -> None:
        path = self.root / name
        line = json.dumps({"date": date, **payload}, ensure_ascii=False,
                          separators=(",", ":"))
        # 同日覆盖：重写该文件，去掉旧同日行再追加
        lines = []
        if path.exists():
            for ln in path.read_text(encoding="utf-8").splitlines():
                try:
                    if json.loads(ln).get("date") != date:
                        lines.append(ln)
                except json.JSONDecodeError:
                    continue
        lines.append(line)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _read_daily(self, name: str, limit: int = 400) -> list[dict]:
        path = self.root / name
        if not path.exists():
            return []
        out = []
        for ln in path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
        return out[-limit:]

    # —— 份额 ——

    def append_shares_day(self, date: str, shares: dict[str, float]) -> None:
        self._append_daily("shares.jsonl", date, {"shares": shares})

    def shares_map(self) -> dict[str, dict]:
        """{code: {code, date, shares, prev_shares, chg_pct}}（与 db.share_map 同义）"""
        rows = self._read_daily("shares.jsonl")
        if not rows:
            return {}
        cur, prev = rows[-1], (rows[-2] if len(rows) >= 2 else {"shares": {}})
        cur_s, prev_s = cur.get("shares", {}), prev.get("shares", {})
        out = {}
        for code, sh in cur_s.items():
            p = prev_s.get(code)
            chg = None
            if sh is not None and p not in (None, 0):
                chg = (sh - p) / p * 100.0
            out[code] = {"code": code, "date": cur.get("date"),
                         "shares": sh, "prev_shares": p, "chg_pct": chg}
        return out

    # —— 限额 ——

    def append_limits_day(self, date: str, limits: dict[str, dict]) -> None:
        self._append_daily("limits.jsonl", date, {"limits": limits})

    def limits_map(self) -> dict[str, dict]:
        rows = self._read_daily("limits.jsonl")
        if not rows:
            return {}
        cur, prev = rows[-1], (rows[-2] if len(rows) >= 2 else {"limits": {}})
        cur_l, prev_l = cur.get("limits", {}), prev.get("limits", {})
        out = {}
        for code, x in cur_l.items():
            p = prev_l.get(code, {})
            ca, pa = x.get("max_amt"), p.get("max_amt")
            chg = None
            if ca is not None and pa not in (None, 0):
                chg = (ca - pa) / pa * 100.0
            out[code] = {"code": code, "date": cur.get("date"),
                         "sgzt": x.get("sgzt"), "max_amt": ca,
                         "prev_max_amt": pa, "prev_sgzt": p.get("sgzt"),
                         "chg_pct": chg}
        return out

    # —— 估算误差（β 校准） ——

    def append_errors_day(self, date: str, pairs: dict[str, list]) -> None:
        """pairs: {code: [est_nav, official_nav]}（同日配对，供 β 比值回归）"""
        self._append_daily("errors.jsonl", date, {"errs": pairs})

    def recent_errors(self, code: str, limit: int = 5) -> list[dict]:
        code = str(code).zfill(6)
        out = []
        for row in reversed(self._read_daily("errors.jsonl")):
            pair = row.get("errs", {}).get(code)
            if pair:
                est, off = pair[0], pair[1]
                if est and off:
                    out.append({"date": row["date"],
                                "err_pct": (est - off) / off * 100.0})
            if len(out) >= limit:
                break
        return out

    def error_pairs(self) -> dict[str, list[list]]:
        """{code: [[est_nav, official_nav], ...]}（按日期升序，供 β 回归）"""
        out: dict[str, list[list]] = {}
        for row in self._read_daily("errors.jsonl"):
            for code, pair in row.get("errs", {}).items():
                if pair and pair[0] and pair[1]:
                    out.setdefault(code, []).append(pair)
        return out
