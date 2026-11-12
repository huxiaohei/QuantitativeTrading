# pyright: reportUnknownLambdaType=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportMissingTypeStubs=false
"""AKShare 数据探查脚本：对每类所需数据实际调用已安装版本的接口，打印列名、行数、日期范围。

运行：uv run python scripts/probe_akshare.py
单个接口失败只打印错误，不中断；东方财富 push2 接口会限流，故调用之间留有间隔。
"""

from __future__ import annotations

import datetime
import time
from collections.abc import Callable
from typing import Any

import akshare as ak
import pandas as pd

PAUSE = 2.0  # 秒，调用间隔，避免被限流
START = "20090101"
END = datetime.date.today().strftime("%Y%m%d")  # 结束日期取运行当天
MAIN_PREFIX = ("600", "601", "603", "605", "000", "001", "002", "003")
CACHE: dict[str, pd.DataFrame] = {}  # 供后续探查复用的已取数据


def show(
    name: str, fn: Callable[[], pd.DataFrame], date_col: str | None = None
) -> pd.DataFrame | None:
    """调用接口并打印概况；失败返回 None。"""
    t0 = time.time()
    try:
        df = fn()
    except Exception as exc:  # 探查脚本需要容忍任何接口失败
        print(f"== {name}: 失败 {type(exc).__name__}: {str(exc)[:160]}")
        time.sleep(PAUSE)
        return None
    sec = time.time() - t0
    print(f"== {name}: 成功 {sec:.2f}s 行数={len(df)}")
    CACHE[name] = df
    print(f"   列名={list(df.columns)}")
    if date_col is not None and date_col in df.columns and len(df) > 0:
        print(f"   {date_col} 范围={df[date_col].min()} ~ {df[date_col].max()}")
    time.sleep(PAUSE)
    return df


def probe_calendar() -> None:
    show("交易日历 tool_trade_date_hist_sina", ak.tool_trade_date_hist_sina, "trade_date")


def probe_securities() -> tuple[pd.DataFrame | None, pd.DataFrame | None]:
    show("在市 stock_info_a_code_name", ak.stock_info_a_code_name)
    sh = show(
        "沪市 stock_info_sh_name_code(主板A股)",
        lambda: ak.stock_info_sh_name_code(symbol="主板A股"),
        "上市日期",
    )
    sz = show(
        "深市 stock_info_sz_name_code(A股列表)",
        lambda: ak.stock_info_sz_name_code(symbol="A股列表"),
        "A股上市日期",
    )
    if sz is not None:
        print(f"   深市板块计数: {sz['板块'].value_counts().to_dict()}")
    shd = show(
        "沪市退市 stock_info_sh_delist(全部)",
        lambda: ak.stock_info_sh_delist(symbol="全部"),
        "暂停上市日期",
    )
    szd = show(
        "深市退市 stock_info_sz_delist(终止上市公司)",
        lambda: ak.stock_info_sz_delist(symbol="终止上市公司"),
        "终止上市日期",
    )
    for key, frame in (("沪市在市", sh), ("深市在市", sz), ("沪市退市", shd), ("深市退市", szd)):
        if frame is not None:
            CACHE[key] = frame
    if shd is not None:
        print(f"   沪市退市代码前缀: {shd['公司代码'].str[:3].value_counts().to_dict()}")
    if szd is not None:
        print(f"   深市退市代码前缀: {szd['证券代码'].str[:3].value_counts().to_dict()}")
    return shd, szd


def pick_delisted(shd: pd.DataFrame | None, szd: pd.DataFrame | None) -> list[str]:
    """选主板已退市样本：沪市 2014 年后暂停上市、深市 2014 年后终止上市各取最多 2 只。"""
    out: list[str] = []
    if shd is not None:
        m = shd[
            shd["公司代码"].str.startswith(("600", "601", "603", "605"))
            & (shd["暂停上市日期"].astype(str) >= "2014-01-01")
        ]
        out += list(m["公司代码"].head(2))
    if szd is not None:
        m = szd[
            szd["证券代码"].str.startswith(("000", "001", "002", "003"))
            & (szd["终止上市日期"].astype(str) >= "2014-01-01")
        ]
        out += list(m["证券代码"].head(2))
    return out


def prefix(sym: str) -> str:
    return ("sh" if sym.startswith("6") else "sz") + sym


def probe_bars(delisted: list[str]) -> None:
    print("\n##### 日线 #####")
    samples = ["600000", "000001", *delisted]
    for sym in samples:
        for adj in ("", "hfq"):
            show(
                f"新浪 stock_zh_a_daily {sym} adjust='{adj}'",
                lambda s=sym, a=adj: ak.stock_zh_a_daily(
                    symbol=prefix(s), start_date=START, end_date=END, adjust=a
                ),
                "date",
            )
    show(
        "新浪 hfq-factor 600000",
        lambda: ak.stock_zh_a_daily(symbol="sh600000", adjust="hfq-factor"),
        "date",
    )


def probe_adjust_consistency() -> None:
    """比较东财与新浪的后复权口径，并用除权日验证复权因子是否含现金分红。"""
    print("\n##### 复权口径对比 600000 #####")
    try:
        em_raw = ak.stock_zh_a_hist("600000", "daily", START, END, "")
        time.sleep(PAUSE)
        em_hfq = ak.stock_zh_a_hist("600000", "daily", START, END, "hfq")
        time.sleep(PAUSE)
        sn_raw = ak.stock_zh_a_daily("sh600000", START, END, "")
        sn_hfq = ak.stock_zh_a_daily("sh600000", START, END, "hfq")
    except Exception as exc:
        print(f"   失败 {type(exc).__name__}: {str(exc)[:160]}")
        return
    em = pd.DataFrame({"raw": em_raw["收盘"].to_numpy(), "hfq": em_hfq["收盘"].to_numpy()})
    em.index = pd.to_datetime(em_raw["日期"])
    sn = pd.DataFrame({"raw": sn_raw["close"].to_numpy(), "hfq": sn_hfq["close"].to_numpy()})
    sn.index = pd.to_datetime(sn_raw["date"])
    v_em, v_sn = em_raw["成交量"].iloc[0], sn_raw["volume"].iloc[0]
    print(f"   2009-01-05 成交量 东财={v_em} 手, 新浪={v_sn:.0f} 股")
    for label, d in (("东财", em), ("新浪", sn)):
        f = d["hfq"] / d["raw"]
        print(f"   {label} adj_factor 首日={f.iloc[0]:.4f} 末日={f.iloc[-1]:.4f}")
    # 除权日 2024-07-18：每股现金红利 0.321 元（派息 3.21 元/10 股）
    ex = pd.Timestamp("2024-07-18")
    for label, d in (("东财", em), ("新浪", sn)):
        i = int(d.index.searchsorted(ex))
        prev_raw = d["raw"].iloc[i - 1]
        f_prev = d["hfq"].iloc[i - 1] / d["raw"].iloc[i - 1]
        f_ex = d["hfq"].iloc[i] / d["raw"].iloc[i]
        implied = prev_raw * (1 - f_prev / f_ex)
        print(f"   {label} 除权日因子 {f_prev:.4f}->{f_ex:.4f} 隐含红利≈{implied:.3f}(实际0.321)")
    time.sleep(PAUSE)


def probe_timing() -> None:
    """对 3 只股票实测单次调用耗时，用于估算全市场下载时间。"""
    print("\n##### 耗时实测 #####")
    syms = ["600519", "000651", "601398", "000002", "600036"]
    for label, fn in (("新浪", lambda s: ak.stock_zh_a_daily(prefix(s), START, END, "")),):
        times: list[float] = []
        fails = 0
        for s in syms:
            t0 = time.time()
            try:
                fn(s)
                times.append(time.time() - t0)
            except Exception as exc:
                fails += 1
                print(f"   {label} {s} 失败 {type(exc).__name__}: {str(exc)[:80]}")
                break
            time.sleep(0.5)
        if times:
            avg = sum(times) / len(times)
            est = avg * 3200 * 2 / 3600
            print(f"   {label} 成功{len(times)}次 平均{avg:.2f}s/次 估算6400次≈{est:.1f}小时")


def probe_dividends() -> None:
    print("\n##### 分红 #####")
    d = show(
        "新浪 stock_history_dividend_detail 600000",
        lambda: ak.stock_history_dividend_detail(symbol="600000", indicator="分红"),
        "公告日期",
    )
    if d is not None:
        print(d.head(3).to_string())
    e = show(
        "东财 stock_fhps_detail_em 600000",
        lambda: ak.stock_fhps_detail_em(symbol="600000"),
        "报告期",
    )
    if e is not None:
        print(
            e[
                [
                    "报告期",
                    "业绩披露日期",
                    "预案公告日",
                    "除权除息日",
                    "最新公告日期",
                    "现金分红-现金分红比例",
                ]
            ]
            .head(3)
            .to_string()
        )
    for sym in ("000001", "600001"):
        show(
            f"东财 stock_fhps_detail_em {sym}",
            lambda s=sym: ak.stock_fhps_detail_em(symbol=s),
            "报告期",
        )
    show(
        "新浪分红 已退市 600001",
        lambda: ak.stock_history_dividend_detail(symbol="600001", indicator="分红"),
        "公告日期",
    )


def probe_financials() -> None:
    print("\n##### 财务 #####")
    f = show(
        "新浪 stock_financial_analysis_indicator 600000",
        lambda: ak.stock_financial_analysis_indicator(symbol="600000", start_year="2009"),
        "日期",
    )
    if f is not None:
        keep = [
            c for c in f.columns if "净资产收益率" in c or "资产负债率" in c or "净利润增长率" in c
        ]
        print(f"   相关列={keep}")
    t = show(
        "同花顺 stock_financial_abstract_ths 600000",
        lambda: ak.stock_financial_abstract_ths(symbol="600000", indicator="按报告期"),
        "报告期",
    )
    if t is not None:
        print(
            t[["报告期", "净利润", "净利润同比增长率", "净资产收益率", "资产负债率"]]
            .tail(3)
            .to_string()
        )
    if f is not None and t is not None:
        fs = f[f["日期"].astype(str) == "2026-09-30"]
        ts = t[t["报告期"].astype(str) == "2026-09-30"]
        if len(fs) and len(ts):
            roe_t, roe_s = ts["净资产收益率"].iloc[0], ts["净资产收益率-摊薄"].iloc[0]
            roe_a, roe_w = fs["净资产收益率(%)"].iloc[0], fs["加权净资产收益率(%)"].iloc[0]
            dr_t, dr_a = ts["资产负债率"].iloc[0], fs["资产负债率(%)"].iloc[0]
            print(f"   2026-09-30 同花顺 ROE={roe_t} 摊薄={roe_s} 负债率={dr_t}")
            print(f"   2026-09-30 新浪 ROE={roe_a} 加权={roe_w} 负债率={dr_a}")
    for sym in ("000004", "000005"):
        g = show(
            f"新浪 stock_financial_analysis_indicator {sym}",
            lambda s=sym: ak.stock_financial_analysis_indicator(symbol=s, start_year="2009"),
            "日期",
        )
        if g is not None:
            nan = int(g["净利润增长率(%)"].isna().sum())
            print(f"   {sym} 净利润增长率(%) 为 NaN 的行={nan}/{len(g)}")
    for date in ("20091231", "20100331", "20141231", "20241231"):
        y = show(
            f"东财 stock_yjbb_em {date}", lambda d=date: ak.stock_yjbb_em(date=d), "最新公告日期"
        )
        if y is not None:
            n_main = int(
                y["股票代码"]
                .str.startswith(("600", "601", "603", "605", "000", "001", "002", "003"))
                .sum()
            )
            print(f"   主板代码行数={n_main}")
    for date in ("20100331", "20241231"):
        show(f"东财 stock_zcfz_em {date}", lambda d=date: ak.stock_zcfz_em(date=d), "公告日期")


def probe_disclosure() -> None:
    print("\n##### 财报实际披露日（巨潮预约披露） #####")
    mb = ("600", "601", "603", "605", "000", "001", "002", "003")
    for period in ("2009年报", "2010一季", "2013年报", "2025年报"):
        d = show(
            f"巨潮 stock_report_disclosure {period}",
            lambda p=period: ak.stock_report_disclosure(market="沪深京", period=p),
            "实际披露",
        )
        if d is not None:
            main = d[d["股票代码"].str.startswith(mb)]
            print(f"   主板行数={len(main)} 实际披露为空={int(main['实际披露'].isna().sum())}")
    for period in ("2009一季", "2009半年报", "2012半年报", "2012三季", "2012年报", "2013年报"):
        d2 = show(
            f"巨潮 stock_report_disclosure {period}（统计）",
            lambda p=period: ak.stock_report_disclosure(market="沪深京", period=p),
            "实际披露",
        )
        if d2 is not None:
            codes = set(d2["股票代码"])
            n_main = int(d2["股票代码"].str.startswith(mb).sum())
            n_null = int(d2["实际披露"].isna().sum())
            flags = {c: c in codes for c in ("600001", "000005", "600068", "000004")}
            print(f"   主板行数={n_main} 实际披露为空={n_null} 退市样本是否在表中={flags}")
    show(
        "同花顺 stock_financial_abstract_ths 已退市 000005",
        lambda: ak.stock_financial_abstract_ths(symbol="000005", indicator="按报告期"),
        "报告期",
    )
    show(
        "新浪 stock_financial_analysis_indicator 已退市 000005",
        lambda: ak.stock_financial_analysis_indicator(symbol="000005", start_year="2009"),
        "日期",
    )


def probe_industry() -> None:
    print("\n##### 行业 #####")
    show("申万历史 stock_industry_clf_hist_sw", ak.stock_industry_clf_hist_sw, "start_date")
    c = show(
        "巨潮 stock_industry_change_cninfo 600000",
        lambda: ak.stock_industry_change_cninfo(symbol="600000", start_date=START, end_date=END),
        "变更日期",
    )
    if c is not None:
        print(c[["分类标准", "行业门类", "行业大类", "变更日期"]].to_string())
    for sym in ("000001", "000005"):
        c2 = show(
            f"巨潮 stock_industry_change_cninfo {sym}",
            lambda s=sym: ak.stock_industry_change_cninfo(
                symbol=s, start_date="19900101", end_date=END
            ),
            "变更日期",
        )
        if c2 is not None:
            print(c2[["分类标准", "行业门类", "变更日期"]].to_string())


def probe_st() -> None:
    print("\n##### ST 状态 #####")
    r = show(
        "新浪 stock_info_change_name 600001", lambda: ak.stock_info_change_name(symbol="600001")
    )
    if r is not None:
        print(r.to_string())
    r2 = show(
        "新浪 stock_info_change_name 000005", lambda: ak.stock_info_change_name(symbol="000005")
    )
    if r2 is not None:
        print(r2.to_string())


def probe_benchmarks() -> None:
    print("\n##### 基准 #####")
    show(
        "中证 stock_zh_index_hist_csindex H00300",
        lambda: ak.stock_zh_index_hist_csindex(symbol="H00300", start_date=START, end_date=END),
        "日期",
    )
    show(
        "新浪 fund_etf_hist_sina sh510880",
        lambda: ak.fund_etf_hist_sina(symbol="sh510880"),
        "date",
    )
    show(
        "新浪 fund_etf_dividend_sina sh510880",
        lambda: ak.fund_etf_dividend_sina(symbol="sh510880"),
        "日期",
    )


def main_board_delisted() -> list[tuple[str, str]]:
    """2013 年及以后退市的主板股票 (代码, 退市日期)，来自沪深退市表。"""
    out: list[tuple[str, str]] = []
    shd = CACHE.get("沪市退市")
    szd = CACHE.get("深市退市")
    if shd is not None:
        for code, dt in zip(shd["公司代码"], shd["暂停上市日期"], strict=True):
            if str(code).startswith(("600", "601", "603", "605")) and str(dt) >= "2013-01-01":
                out.append((str(code), str(dt)))
    if szd is not None:
        for code, dt in zip(szd["证券代码"], szd["终止上市日期"], strict=True):
            if str(code).startswith(("000", "001", "002", "003")) and str(dt) >= "2013-01-01":
                out.append((str(code), str(dt)))
    return out


def probe_sina_rate() -> None:
    """连续 20 次调用新浪日线，观察是否限流。"""
    print("\n##### 新浪连续调用 20 次 #####")
    codes = [
        "600036", "600519", "601398", "601318", "600030", "600016", "600028", "600048",
        "600104", "600585", "601088", "601166", "601288", "601328", "601628", "601668",
        "601857", "601988", "600009", "600010",
    ]  # fmt: skip
    t0 = time.time()
    ok = 0
    for c in codes:
        try:
            ak.stock_zh_a_daily(symbol=prefix(c), start_date=START, end_date=END, adjust="")
            ok += 1
        except Exception as exc:
            print(f"   {c} 失败 {type(exc).__name__}: {str(exc)[:100]}")
            break
    print(f"   连续成功 {ok}/20 次，共 {time.time() - t0:.1f}s")


def probe_hfq_factor() -> None:
    """新浪复权因子校验：与未复权/后复权价之比对照，并用除息日反推每股红利。"""
    print("\n##### 新浪 hfq-factor 校验 600000 #####")
    try:
        raw = ak.stock_zh_a_daily("sh600000", START, END, "")
        hfq = ak.stock_zh_a_daily("sh600000", START, END, "hfq")
        fac = ak.stock_zh_a_daily("sh600000", adjust="hfq-factor")
    except Exception as exc:
        print(f"   失败 {type(exc).__name__}: {str(exc)[:160]}")
        return
    idx = pd.to_datetime(raw["date"])
    ratio = pd.Series(hfq["close"].to_numpy() / raw["close"].to_numpy(), index=idx)
    ev = pd.Series(
        fac["hfq_factor"].astype(float).to_numpy(), index=pd.to_datetime(fac["date"])
    ).sort_index()
    exact = ev.reindex(ev.index.union(pd.DatetimeIndex(idx))).ffill().reindex(idx)
    print(f"   hfq_factor 列类型={fac['hfq_factor'].dtype} 事件行数={len(fac)}")
    print(f"   价格之比 首日={ratio.iloc[0]:.4f} 末日={ratio.iloc[-1]:.4f}")
    print(f"   事件表前向填充 首日={exact.iloc[0]:.6f} 末日={exact.iloc[-1]:.6f}")
    print(f"   价格之比与事件表的最大相对偏差={float((ratio / exact - 1).abs().max()):.5f}")
    vol0, amt0, c0 = raw["volume"].iloc[0], raw["amount"].iloc[0], raw["close"].iloc[0]
    print(f"   volume 首日={vol0:.0f} 股, 成交额/成交量={amt0 / vol0:.2f}, 收盘={c0}")
    ex = pd.Timestamp("2024-07-18")
    i = int(idx.searchsorted(ex))
    prev_close = float(raw["close"].iloc[i - 1])
    f_prev, f_ex = float(exact.iloc[i - 1]), float(exact.iloc[i])
    print(
        f"   2024-07-18 事件表因子 {f_prev:.6f}->{f_ex:.6f}，前收 {prev_close}，"
        f"反推每股红利={prev_close * (1 - f_prev / f_ex):.4f}（分红表 3.21 元/10 股=0.321）"
    )
    cal = pd.to_datetime(ak.tool_trade_date_hist_sina()["trade_date"])
    cal = cal[(cal >= idx.min()) & (cal <= idx.max())]
    print(
        f"   日历交易日={len(cal)} 日线行数={len(raw)} 日历有而日线无={len(set(cal) - set(idx))}"
        f" 成交量为0的行={int((raw['volume'] == 0).sum())}"
    )
    time.sleep(PAUSE)


def probe_delisted_bars_sample() -> None:
    """抽样 10 只 2013 年后退市的主板股票，统计新浪日线能否取到（全量缺失数未测量）。"""
    print("\n##### 已退市股票日线抽样（新浪） #####")
    pool = sorted(main_board_delisted())
    if not pool:
        print("   退市表不可用")
        return
    step = max(1, len(pool) // 10)
    sample = pool[::step][:10]
    got = 0
    for code, dt in sample:
        try:
            d = ak.stock_zh_a_daily(symbol=prefix(code), start_date=START, end_date=END, adjust="")
            n = len(d)
            last = d["date"].max() if n else None
            print(f"   {code} 退市表日期={dt} 日线行数={n} 最后一行={last}")
            got += int(n > 0)
        except Exception as exc:
            print(f"   {code} 退市表日期={dt} 失败 {type(exc).__name__}: {str(exc)[:80]}")
        time.sleep(1.0)
    print(f"   抽样 {len(sample)}（池{len(pool)}只）可取到 {got}/{len(sample)}；全量缺失数未测量")


def derive_fiscal_year(ex_dates: list[pd.Timestamp]) -> list[int]:
    """由除权日推断会计年度：1 到 8 月除权记上一年（年报），9 到 12 月记当年（中期）。"""
    return [d.year - 1 if d.month <= 8 else d.year for d in ex_dates]


DIV_DEV_SAMPLE = [
    "600000", "000001", "600036", "600519", "000651", "000333", "601088", "600028",
    "601318", "000858", "601398", "600900", "600309", "000002", "002415", "601166",
]  # fmt: skip
DIV_HOLDOUT_SAMPLE = [
    "600104", "601939", "601328", "600050", "000063", "000725", "002594", "601857",
    "600887", "000538", "600276", "601899",
]  # fmt: skip


def fiscal_year_agreement(symbols: list[str]) -> tuple[int, int, int, int, list[str]]:
    """对每只股票比较 Sina 推断的会计年度与东财 `报告期`（按除权除息日匹配）。

    返回 (匹配行数, 逐行一致数, 会计年度合计比较数, 年度合计一致数, 不一致明细)。
    """
    matched = agree = fy_total = fy_agree = 0
    fails: list[str] = []
    for s in symbols:
        try:
            d = ak.stock_history_dividend_detail(symbol=s, indicator="分红")
            time.sleep(1.0)
            e = ak.stock_fhps_detail_em(symbol=s)
            time.sleep(PAUSE)
        except Exception as exc:
            print(f"   {s} 失败 {type(exc).__name__}: {str(exc)[:80]}")
            continue
        d = d[
            (d["进度"] == "实施") & d["除权除息日"].notna() & (d["派息"].astype(float) > 0)
        ].copy()
        d["ex"] = pd.to_datetime(d["除权除息日"])
        d = d.sort_values("ex")
        e = e[e["除权除息日"].notna()].copy()
        e["ex"] = pd.to_datetime(e["除权除息日"])
        m = d.merge(e[["ex", "报告期"]], on="ex", how="inner")
        m["truth"] = pd.to_datetime(m["报告期"]).dt.year
        m["rule"] = derive_fiscal_year(list(m["ex"]))
        m["cash"] = m["派息"].astype(float) / 10
        matched += len(m)
        agree += int((m["rule"] == m["truth"]).sum())
        for _, r in m[m["rule"] != m["truth"]].iterrows():
            fails.append(
                f"{s} 除权日={r['ex'].date()} 东财报告期={str(r['报告期'])[:10]} 推断={r['rule']}"
            )
        a = m.groupby("rule")["cash"].sum().round(4)
        b = m.groupby("truth")["cash"].sum().round(4)
        both = a.index.intersection(b.index)
        fy_total += len(both)
        fy_agree += int(((a[both] - b[both]).abs() < 1e-6).sum())
    return matched, agree, fy_total, fy_agree, fails


def probe_fiscal_year() -> None:
    print("\n##### fiscal_year 推断规则验证（新浪除权日 vs 东财报告期） #####")
    for label, syms in (
        ("开发样本（规则据此调整）", DIV_DEV_SAMPLE),
        ("留出样本", DIV_HOLDOUT_SAMPLE),
    ):
        matched, agree, fy_total, fy_agree, fails = fiscal_year_agreement(syms)
        print(f"   {label} 股票数={len(syms)} 匹配行={matched} 逐行一致={agree}")
        print(f"   {label} 年度合计可比={fy_total} 年度合计一致={fy_agree}")
        for f in fails[:30]:
            print(f"     不一致: {f}")
    r = show(
        "新浪分红 000005",
        lambda: ak.stock_history_dividend_detail(symbol="000005", indicator="分红"),
        "公告日期",
    )
    if r is not None:
        print(f"   进度取值={r['进度'].value_counts().to_dict()}")


def probe_industry_checks() -> None:
    """巨潮行业接口的补充检查：早起始日、证监会 2012 标准行、变更日期与上市日期对比。"""
    print("\n##### 巨潮行业补充检查 #####")
    list_dates: dict[str, str] = {}
    sh = CACHE.get("沪市在市")
    sz = CACHE.get("深市在市")
    if sh is not None:
        list_dates.update(dict(zip(sh["证券代码"], sh["上市日期"].astype(str), strict=True)))
    if sz is not None:
        list_dates.update(dict(zip(sz["A股代码"], sz["A股上市日期"].astype(str), strict=True)))
    sample = [
        "600000",
        "600519",
        "601398",
        "000001",
        "000002",
        "002415",
        "600036",
        "000651",
        "601318",
        "600028",
        "600068",
        "600001",
        "000005",
    ]
    has2012 = 0
    asked = 0
    for c in sample:
        try:
            d = ak.stock_industry_change_cninfo(symbol=c, start_date="19900101", end_date=END)
        except Exception as exc:
            print(f"   {c} 失败/空表 {type(exc).__name__}: {str(exc)[:60]}")
            time.sleep(1.5)
            continue
        asked += 1
        csrc = d[d["分类标准"].astype(str).str.contains("证监会行业分类标准（2012）")]
        has2012 += int(len(csrc) > 0)
        first = str(csrc["变更日期"].min()) if len(csrc) else "无"
        last = f"{csrc.iloc[-1]['行业门类']}/{csrc.iloc[-1]['行业大类']}" if len(csrc) else "无"
        print(
            f"   {c} 总行数={len(d)} 证监会2012行数={len(csrc)} 其最早变更日期={first} "
            f"上市日期={list_dates.get(c, '未知')} 门类/大类={last}"
        )
        time.sleep(1.5)
    print(f"   有数据的 {asked} 只中含证监会2012标准行的={has2012}")


def probe_misc_claims() -> None:
    """复现文档中的其余数字：ST 名称数、H00300 空列、510300 起始日、东财整市场财报接口。"""
    print("\n##### 其余复现项 #####")
    a = CACHE.get("在市 stock_info_a_code_name")
    if a is not None:
        print(f"   当前名称含 ST 的数量={int(a['name'].str.contains('ST').sum())}")
    h = CACHE.get("中证 stock_zh_index_hist_csindex H00300")
    if h is not None:
        ohl = [int(h[c].notna().sum()) for c in ("开盘", "最高", "最低")]
        print(f"   H00300 开盘/最高/最低非空行数={ohl}")
        print(f"   H00300 前两行 收盘={list(h['收盘'].iloc[:2])} 日期={list(h['日期'].iloc[:2])}")
    show(
        "新浪 fund_etf_hist_sina sh510300", lambda: ak.fund_etf_hist_sina(symbol="sh510300"), "date"
    )
    show(
        "新浪 fund_etf_dividend_sina sh510300",
        lambda: ak.fund_etf_dividend_sina(symbol="sh510300"),
        "日期",
    )
    delisted = [c for c, _ in main_board_delisted()]
    dl = [c for c, dt in main_board_delisted() if dt >= "2016-01-01"]
    deadline = {"1231": "-04-30"}
    for date, year_shift in (("20091231", 1), ("20141231", 1)):
        y = show(
            f"东财 stock_yjbb_em {date}（公告日期与退市覆盖）",
            lambda d=date: ak.stock_yjbb_em(date=d),
            "最新公告日期",
        )
        if y is None:
            continue
        ann = pd.to_datetime(y["最新公告日期"])
        limit = pd.Timestamp(f"{int(date[:4]) + year_shift}{deadline['1231']}")
        codes = set(y["股票代码"])
        hit = sum(c in codes for c in dl)
        late = float((ann > limit).mean())
        print(f"   {date} 公告日晚于法定截止日的比例={late:.3f} 中位公告日={ann.median().date()}")
        print(
            f"   {date} 2016 年后退市主板股在该表中的覆盖={hit}/{len(dl)}（退市池 {len(delisted)}）"
        )


def probe_em_push2() -> None:
    """东财 push2 系列接口放在最后：本机连续调用数次后会被限流（见 docs/data-gaps.md）。"""
    print("\n##### 东财 push2 接口（易限流，放最后） #####")
    for adj in ("", "hfq"):
        show(
            f"东财 stock_zh_a_hist 600000 adjust='{adj}'",
            lambda a=adj: ak.stock_zh_a_hist(
                symbol="600000", period="daily", start_date=START, end_date=END, adjust=a
            ),
            "日期",
        )
    probe_adjust_consistency()
    for sym in ("510300", "510880"):
        show(
            f"东财 fund_etf_hist_em {sym} hfq",
            lambda s=sym: ak.fund_etf_hist_em(
                symbol=s, period="daily", start_date=START, end_date=END, adjust="hfq"
            ),
            "日期",
        )
    n = show("东财 stock_board_industry_name_em", ak.stock_board_industry_name_em)
    if n is not None:
        name = str(n.iloc[0]["板块名称"])
        show(
            f"东财 stock_board_industry_cons_em {name}",
            lambda: ak.stock_board_industry_cons_em(symbol=name),
        )
    show("东财 stock_zh_a_st_em（仅当前）", ak.stock_zh_a_st_em)


def run(step: Callable[[], object]) -> None:
    """运行一个探查步骤；任何异常只打印，不影响后续步骤。"""
    try:
        step()
    except Exception as exc:
        name = getattr(step, "__name__", "step")
        print(f"!! 步骤 {name} 异常 {type(exc).__name__}: {str(exc)[:160]}")


def main() -> None:
    ver: Any = ak.__version__
    print(f"AKShare 版本: {ver}  运行日期: {datetime.date.today()}")
    run(probe_calendar)
    run(probe_securities)
    delisted = pick_delisted(CACHE.get("沪市退市"), CACHE.get("深市退市"))
    print(f"已退市主板样本: {delisted}")
    run(probe_dividends)
    run(probe_fiscal_year)
    run(probe_financials)
    run(probe_disclosure)
    run(probe_industry)
    run(probe_industry_checks)
    run(probe_st)
    run(probe_benchmarks)
    run(lambda: probe_bars(delisted))
    run(probe_hfq_factor)
    run(probe_delisted_bars_sample)
    run(probe_sina_rate)
    run(probe_timing)
    run(probe_misc_claims)
    run(probe_em_push2)


if __name__ == "__main__":
    main()
