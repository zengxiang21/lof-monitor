"""test_shares.py — 份额刷新鲁棒性回归

覆盖 update_all_shares 的三级数据源编排：
1. 双所皆败 → 跳过 EM 兜底（防止 390 次单只请求的长尾雪崩），保留旧数据
2. 单所得失 → 仅未收录代码走 EM 兜底
3. SSE 沪市场景 → 同 2
另覆盖 _parse_sse_lofgm 解析（真实响应结构、万份→份、坏负载抛错）。
"""
import app.shares as sh

PASS = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")
    if not cond:
        raise SystemExit(f"FAILED: {name} {detail}")
    PASS.append(name)


class FakeDB:
    def __init__(self, codes):
        self._codes = codes
        self.calls = []

    def known_codes(self):
        return self._codes

    def upsert_shares(self, code, date, shares, source="szse"):
        self.calls.append((code, source))

    def log(self, *a):
        pass


def _boom(*a, **k):
    raise OSError("net down")


CODES = [f"{500000+i}" for i in range(20)]
_orig = (sh.fetch_szse_shares, sh.fetch_sse_shares, sh.fetch_em_share)

# —— 场景 1：双所皆败 → 跳过 EM 兜底 ——
sh.fetch_szse_shares = _boom
sh.fetch_sse_shares = _boom
sh.fetch_em_share = lambda code: ("2026-10-23", 1000.0)
db = FakeDB(CODES)
st = sh.update_all_shares(db)
check("双所失败→skipped", st["skipped"] is True, str(st))
check("双所失败→零写入", st["ok"] == 0 and st["fail"] == 20 and not db.calls,
      f"(calls={len(db.calls)})")

# —— 场景 2：SZSE 成功、SSE 失败 → 仅未收录走 EM ——
sh.fetch_szse_shares = lambda: {"500000": 100.0, "500001": 200.0}
sh.fetch_sse_shares = _boom
db2 = FakeDB(CODES)
st2 = sh.update_all_shares(db2)
check("SZSE-only→EM 补 18 只", st2["ok"] == 20 and st2["em"] == 18
      and st2["szse"] == 2 and not st2["skipped"], str(st2))
check("SZSE-only→来源正确", sum(1 for _, s in db2.calls if s == "szse") == 2
      and sum(1 for _, s in db2.calls if s == "em") == 18)

# —— 场景 3：SSE 成功、SZSE 失败（沪市场景）——
sh.fetch_szse_shares = _boom
sh.fetch_sse_shares = lambda *a: ("2026-10-23", {"500002": 300.0})
db3 = FakeDB(CODES)
st3 = sh.update_all_shares(db3)
check("SSE-only→EM 补 19 只", st3["ok"] == 20 and st3["em"] == 19
      and st3["sse"] == 1, str(st3))
check("SSE-only→快照日期用交易所日期", st3["latest_date"] == "2026-10-23",
      str(st3["latest_date"]))

# —— 场景 4：_parse_sse_lofgm 解析 ——
_mock = ('cb({"pageHelp":{"data":['
         '{"FUND_CODE":"501001","TRADE_DATE":"20261023","INTERNAL_VOL":"508.69"},'
         '{"FUND_CODE":"501005","TRADE_DATE":"20261023","INTERNAL_VOL":"2,761.93"},'
         '{"FUND_CODE":"501099","TRADE_DATE":"20261023","INTERNAL_VOL":"0"}]}});')
snap, m = sh._parse_sse_lofgm(_mock)
check("解析：日期格式", snap == "2026-10-23", snap)
check("解析：万份→份", m.get("501001") == 5086900.0, str(m.get("501001")))
check("解析：千分位+剔零", m.get("501005") == 27619300.0 and "501099" not in m, str(m))
try:
    sh._parse_sse_lofgm("garbage")
    check("解析：坏负载抛错", False)
except ValueError:
    check("解析：坏负载抛错", True)

sh.fetch_szse_shares, sh.fetch_sse_shares, sh.fetch_em_share = _orig
print(f"\nSHARES TESTS PASSED ({len(PASS)} checks)")
