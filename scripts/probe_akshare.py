# pyright: reportUnknownLambdaType=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportMissingTypeStubs=false
"""AKShare 数据探查脚本：对每类所需数据实际调用已安装版本的接口，打印列名、行数、日期范围。

运行：uv run python scripts/probe_akshare.py
单个接口失败只打印错误，不中断；东方财富 push2 接口会限流，故调用之间留有间隔。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import akshare as ak
import pandas as pd

PAUSE = 2.0  # 秒，调用间隔，避免被限流
START = "20090101"
END = "20261112"


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
    print(f"   列名={list(df.columns)}")
    if date_col is not None and date_col in df.columns and len(df) > 0:
        print(f"   {date_col} 范围={df[date_col].min()} ~ {df[date_col].max()}")
    time.sleep(PAUSE)
    return df


def probe_calendar() -> None:
    show("交易日历 tool_trade_date_hist_sina", ak.tool_trade_date_hist_sina, "trade_date")


def probe_securities() -> tuple[pd.DataFrame | None, pd.DataFrame | None]:
    show("在市 stock_info_a_code_name", ak.stock_info_a_code_name)
    show(
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


def main() -> None:
    ver: Any = ak.__version__
    print(f"AKShare 版本: {ver}")
    probe_calendar()
    shd, szd = probe_securities()
    delisted = pick_delisted(shd, szd)
    print(f"已退市主板样本: {delisted}")
    probe_dividends()
    probe_financials()
    probe_disclosure()
    probe_industry()
    probe_st()
    probe_benchmarks()
    probe_bars(delisted)
    probe_timing()
    probe_em_push2()


if __name__ == "__main__":
    main()
