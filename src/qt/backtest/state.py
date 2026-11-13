"""引擎与风控状态的 JSON 序列化：逐日服务每晚读出状态、推进一天、再存回数据库。

规则（保证逐日推进与一次性回测结果逐位相同）：
    - 数据类按字段名逐个序列化，新增带默认值的字段无需改动本模块；
      读取时缺少的字段取数据类默认值（旧状态仍能读），缺少必填字段或出现未知字段直接报错；
    - 浮点数原样保存，不做舍入（json 用 repr 输出有限浮点，读回逐位相同）；
      NaN 与正负无穷不是合法 JSON，存成字符串 "NaN"、"Infinity"、"-Infinity"；
      整数仍存为整数，读回仍是 int，不会变成 float；
    - 日期存 ISO 字符串，枚举存取值；
    - 字典（有序）存为 [键, 值] 对的数组，不依赖 JSON 对象的键序；元组与列表存为数组。
"""

import math
import types
from collections.abc import Callable, Mapping
from dataclasses import MISSING, fields, is_dataclass
from datetime import date, datetime
from enum import Enum
from typing import Union, cast, get_args, get_origin, get_type_hints

from qt.backtest.engine import EngineState
from qt.risk import RiskState

STATE_VERSION = 1
_NON_FINITE = {"NaN": math.nan, "Infinity": math.inf, "-Infinity": -math.inf}


def _encode_float(value: float) -> object:
    if math.isfinite(value):
        return value
    if math.isnan(value):
        return "NaN"
    return "Infinity" if value > 0 else "-Infinity"


def _encode(value: object) -> object:
    """把状态里的值转成只含 JSON 类型（dict、list、str、int、float、bool、None）的结构。"""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, Enum):  # StrEnum 也是 str，须先于 str 判断
        return cast(object, value.value)
    if isinstance(value, int | str):
        return value
    if isinstance(value, float):
        return _encode_float(value)
    if isinstance(value, datetime):
        raise TypeError(f"状态里不应出现 datetime：{value!r}")
    if isinstance(value, date):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _encode(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        items = cast(Mapping[object, object], value).items()
        return [[_encode(k), _encode(v)] for k, v in items]
    if isinstance(value, list | tuple):
        return [_encode(v) for v in cast(list[object] | tuple[object, ...], value)]
    raise TypeError(f"无法序列化的状态值：{value!r}（类型 {type(value).__name__}）")


def _as_list(raw: object, what: str) -> list[object]:
    if not isinstance(raw, list):
        raise ValueError(f"{what} 应为数组，收到 {raw!r}")
    return cast(list[object], raw)


def _as_object(raw: object, what: str) -> Mapping[str, object]:
    if not isinstance(raw, dict):
        raise ValueError(f"{what} 应为对象，收到 {raw!r}")
    return cast(Mapping[str, object], raw)


def _decode_float(raw: object, what: str) -> float:
    if isinstance(raw, str) and raw in _NON_FINITE:
        return _NON_FINITE[raw]
    if isinstance(raw, int | float) and not isinstance(raw, bool):
        return raw  # 整数原样保留为 int，与序列化前的值类型一致
    raise ValueError(f"{what} 应为数值，收到 {raw!r}")


def _decode(raw: object, tp: object, what: str) -> object:
    """按类型标注 tp 把 JSON 值还原；不支持的标注直接报错（新增字段类型时须在此补充）。"""
    origin = get_origin(tp)
    args = get_args(tp)
    if origin is Union or origin is types.UnionType:
        if raw is None and type(None) in args:
            return None
        rest = [a for a in args if a is not type(None)]
        if len(rest) != 1:
            raise TypeError(f"{what}：不支持的联合类型 {tp!r}")
        return _decode(raw, rest[0], what)
    if tp is float:
        return _decode_float(raw, what)
    if tp is bool or tp is int or tp is str:
        if type(raw) is not tp:
            raise ValueError(f"{what} 应为 {cast(type, tp).__name__}，收到 {raw!r}")
        return raw
    if tp is date:
        if not isinstance(raw, str):
            raise ValueError(f"{what} 应为 ISO 日期字符串，收到 {raw!r}")
        return date.fromisoformat(raw)
    if isinstance(tp, type) and issubclass(tp, Enum):
        return tp(raw)
    if isinstance(tp, type) and is_dataclass(tp):
        return _decode_dataclass(tp, raw, what)
    if origin is dict or origin is Mapping:
        key_tp, value_tp = args
        out: dict[object, object] = {}
        for i, item in enumerate(_as_list(raw, what)):
            pair = _as_list(item, f"{what}[{i}]")
            if len(pair) != 2:
                raise ValueError(f"{what}[{i}] 应为 [键, 值]，收到 {item!r}")
            out[_decode(pair[0], key_tp, f"{what}[{i}] 的键")] = _decode(
                pair[1], value_tp, f"{what}[{i}] 的值"
            )
        return out
    if origin is list:
        (item_tp,) = args
        return [_decode(v, item_tp, f"{what}[{i}]") for i, v in enumerate(_as_list(raw, what))]
    if origin is tuple:
        items = _as_list(raw, what)
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_decode(v, args[0], f"{what}[{i}]") for i, v in enumerate(items))
        if len(items) != len(args):
            raise ValueError(f"{what} 应有 {len(args)} 项，收到 {raw!r}")
        return tuple(
            _decode(v, t, f"{what}[{i}]") for i, (v, t) in enumerate(zip(items, args, strict=True))
        )
    raise TypeError(f"{what}：不支持的类型标注 {tp!r}")


def _decode_dataclass[T](cls: type[T], raw: object, what: str) -> T:
    """按字段名还原数据类；缺少的字段取默认值，缺少必填字段或有未知字段时报错。"""
    obj = _as_object(raw, what)
    hints = get_type_hints(cls)
    known = {f.name for f in fields(cast(type, cls))}
    unknown = sorted(set(obj) - known)
    if unknown:
        raise ValueError(f"{what}（{cls.__name__}）有未知字段：{unknown}")
    kwargs: dict[str, object] = {}
    for f in fields(cast(type, cls)):
        if f.name in obj:
            kwargs[f.name] = _decode(obj[f.name], hints[f.name], f"{what}.{f.name}")
        elif f.default is MISSING and f.default_factory is MISSING:
            raise ValueError(f"{what}（{cls.__name__}）缺少必填字段：{f.name}")
    return cast(Callable[..., T], cls)(**kwargs)


def dump_state(state: EngineState, risk: RiskState) -> dict[str, object]:
    """把引擎状态与风控状态转成只含 JSON 类型的字典（有序集合都是数组）。"""
    return {"version": STATE_VERSION, "engine": _encode(state), "risk": _encode(risk)}


def load_state(raw: Mapping[str, object]) -> tuple[EngineState, RiskState]:
    """还原 dump_state 的输出（通常经过 json.loads）。"""
    version = raw.get("version")
    if version != STATE_VERSION:
        raise ValueError(f"不支持的状态版本 {version!r}，当前版本为 {STATE_VERSION}")
    unknown = sorted(set(raw) - {"version", "engine", "risk"})
    if unknown:
        raise ValueError(f"状态有未知字段：{unknown}")
    engine = _decode_dataclass(EngineState, raw.get("engine"), "engine")
    risk = _decode_dataclass(RiskState, raw.get("risk"), "risk")
    return engine, risk
