"""单元自测：受限语法解析、纯 Python 判定、z3 全量兼容判定。
运行：python3 tests/test_units.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from schema_spec import (SchemaError, canonical_hash, canonical_json,
                         message_valid, parse_schema)  # noqa: E402
from solver import VerdictCache, check_compatibility, find_counterexample  # noqa: E402

PASS = []


def ok(name, cond):
    assert cond, f"FAILED: {name}"
    PASS.append(name)


def expect_error(name, doc):
    try:
        parse_schema(doc)
    except SchemaError:
        PASS.append(name)
        return
    raise AssertionError(f"FAILED(应拒绝但未拒绝): {name}")


def S(doc):
    return parse_schema(doc)


# ---------- 语法子集：应接受 ----------
ok("平面对象", S({"type": "object", "properties": {"a": {"type": "integer", "minimum": 1, "maximum": 5}},
                  "required": ["a"], "additionalProperties": False}))
ok("顶层anyOf", S({"anyOf": [
    {"type": "object", "properties": {"a": {"type": "integer"}}, "additionalProperties": False},
    {"type": "object", "properties": {"b": {"enum": ["x", None]}}, "required": ["b"],
     "additionalProperties": False}]}))
ok("null类型", S({"type": "object", "properties": {"q": {"type": "null"}}, "additionalProperties": False}))
ok("混合枚举", S({"type": "object", "properties": {"q": {"enum": ["N", 1, None]}}, "additionalProperties": False}))
ok("整数枚举", S({"type": "object", "properties": {"q": {"type": "integer", "enum": [1, 2]}},
                  "additionalProperties": False}))
ok("无required", S({"type": "object", "properties": {"a": {"type": "integer"}}, "additionalProperties": False}))
ok("无界整数", S({"type": "object", "properties": {"a": {"type": "integer"}}, "additionalProperties": False}))

# ---------- 语法子集：应拒绝 ----------
expect_error("嵌套对象", {"type": "object", "properties": {"a": {"type": "object", "properties": {}}},
                          "additionalProperties": False})
expect_error("数组", {"type": "object", "properties": {"a": {"type": "array", "items": {}}},
                     "additionalProperties": False})
expect_error("pattern", {"type": "object", "properties": {"a": {"type": "string", "pattern": "^x"}},
                         "additionalProperties": False})
expect_error("纯string无enum", {"type": "object", "properties": {"a": {"type": "string"}},
                               "additionalProperties": False})
expect_error("number类型", {"type": "object", "properties": {"a": {"type": "number"}},
                            "additionalProperties": False})
expect_error("布尔枚举", {"type": "object", "properties": {"a": {"enum": [True]}},
                          "additionalProperties": False})
expect_error("浮点枚举", {"type": "object", "properties": {"a": {"enum": [1.5]}},
                          "additionalProperties": False})
expect_error("缺additionalProperties", {"type": "object", "properties": {}})
expect_error("additionalProperties为true", {"type": "object", "properties": {}, "additionalProperties": True})
expect_error("min大于max", {"type": "object", "properties": {"a": {"type": "integer", "minimum": 5, "maximum": 1}},
                            "additionalProperties": False})
expect_error("required未声明", {"type": "object", "properties": {}, "required": ["x"],
                                "additionalProperties": False})
expect_error("enum为空", {"type": "object", "properties": {"a": {"enum": []}}, "additionalProperties": False})
expect_error("enum与界并存", {"type": "object", "properties": {"a": {"type": "integer", "minimum": 1, "enum": [1]}},
                              "additionalProperties": False})
expect_error("anyOf混用", {"anyOf": [{"type": "object", "properties": {}, "additionalProperties": False}],
                            "type": "object"})
expect_error("anyOf分支非对象", {"anyOf": [{"type": "string"}]})
expect_error("顶层非对象", [1, 2])
expect_error("oneOf", {"oneOf": [{"type": "object", "properties": {}, "additionalProperties": False}]})
expect_error("type与enum不一致", {"type": "object", "properties": {"a": {"type": "string", "enum": [1]}},
                                  "additionalProperties": False})

# ---------- 纯 Python 判定 ----------
sch = S({"type": "object",
         "properties": {"t": {"type": "integer", "minimum": 0, "maximum": 10},
                        "q": {"enum": ["N", None]}},
         "required": ["t"], "additionalProperties": False})
ok("判定:合法报文", message_valid({"t": 5}, sch))
ok("判定:带null", message_valid({"t": 5, "q": None}, sch))
ok("判定:越界拒绝", not message_valid({"t": 11}, sch))
ok("判定:多字段拒绝", not message_valid({"t": 5, "x": 1}, sch))
ok("判定:缺必填拒绝", not message_valid({"q": "N"}, sch))
ok("判定:布尔不是整数", not message_valid({"t": True}, sch))
ok("判定:字符串枚举", message_valid({"t": 5, "q": "N"}, sch))
ok("判定:非法枚举值", not message_valid({"t": 5, "q": "Z"}, sch))

# ---------- z3 全量判定 ----------
def fw(lo, hi):
    return S({"type": "object", "properties": {"temp": {"type": "integer", "minimum": lo, "maximum": hi}},
              "required": ["temp"], "additionalProperties": False})


ok("z3:相同格式兼容", find_counterexample(fw(-10, 50), fw(-10, 50)) is None)
ok("z3:子集兼容", find_counterexample(fw(-10, 50), fw(-40, 100)) is None)
cex = find_counterexample(fw(-40, 100), fw(-10, 50))
ok("z3:扩范围给出反例", cex is not None and isinstance(cex.get("temp"), int)
   and -40 <= cex["temp"] <= 100 and not (-10 <= cex["temp"] <= 50))
ok("z3:反例确被读方拒收", cex is not None and message_valid(cex, fw(-40, 100))
   and not message_valid(cex, fw(-10, 50)))

# 质控码缺省：写方 qc 可选，读方 qc 必填
w_qc = S({"type": "object",
          "properties": {"temp": {"type": "integer", "minimum": -40, "maximum": 100},
                         "qc": {"enum": ["N", "Q", None]}},
          "required": ["temp"], "additionalProperties": False})
r_qc_require = S({"type": "object",
                  "properties": {"temp": {"type": "integer", "minimum": -40, "maximum": 100},
                                 "qc": {"enum": ["N", "Q"]}},
                  "required": ["temp", "qc"], "additionalProperties": False})
r_qc_optional = S({"type": "object",
                   "properties": {"temp": {"type": "integer", "minimum": -40, "maximum": 100},
                                  "qc": {"enum": ["N", "Q", None]}},
                   "required": ["temp"], "additionalProperties": False})
cex = find_counterexample(w_qc, r_qc_require)
ok("z3:qc必填反例", cex is not None and ("qc" not in cex or cex["qc"] is None))
ok("z3:qc可选兼容", find_counterexample(w_qc, r_qc_optional) is None)

# additionalProperties：写方多出的字段被读方拒收
w_extra = S({"type": "object",
             "properties": {"temp": {"type": "integer"}, "raw": {"type": "integer"}},
             "required": ["temp"], "additionalProperties": False})
r_no_extra = S({"type": "object", "properties": {"temp": {"type": "integer"}},
                "required": ["temp"], "additionalProperties": False})
cex = find_counterexample(w_extra, r_no_extra)
ok("z3:多余字段反例", cex is not None and "raw" in cex)

# anyOf：分支覆盖才兼容
w_any = S({"anyOf": [
    {"type": "object", "properties": {"a": {"type": "integer", "minimum": 0, "maximum": 5}},
     "required": ["a"], "additionalProperties": False},
    {"type": "object", "properties": {"b": {"enum": ["x"]}},
     "required": ["b"], "additionalProperties": False}]})
r_any_full = S({"anyOf": [
    {"type": "object", "properties": {"a": {"type": "integer", "minimum": 0, "maximum": 9}},
     "required": ["a"], "additionalProperties": False},
    {"type": "object", "properties": {"b": {"enum": ["x", "y"]}},
     "required": ["b"], "additionalProperties": False}]})
r_any_half = S({"anyOf": [
    {"type": "object", "properties": {"a": {"type": "integer", "minimum": 0, "maximum": 9}},
     "required": ["a"], "additionalProperties": False}]})
ok("z3:anyOf全覆盖兼容", find_counterexample(w_any, r_any_full) is None)
cex = find_counterexample(w_any, r_any_half)
ok("z3:anyOf漏分支反例", cex is not None and cex.get("b") == "x")

# 整数枚举 vs 范围
w_set = S({"type": "object", "properties": {"lv": {"type": "integer", "enum": [0, 1, 2, 3]}},
           "required": ["lv"], "additionalProperties": False})
r_range = S({"type": "object", "properties": {"lv": {"type": "integer", "minimum": 0, "maximum": 3}},
             "required": ["lv"], "additionalProperties": False})
r_range2 = S({"type": "object", "properties": {"lv": {"type": "integer", "minimum": 0, "maximum": 2}},
              "required": ["lv"], "additionalProperties": False})
ok("z3:枚举⊆范围", find_counterexample(w_set, r_range) is None)
cex = find_counterexample(w_set, r_range2)
ok("z3:枚举越界反例", cex is not None and cex.get("lv") == 3)

# check_compatibility 汇总
problems = check_compatibility([(1, fw(-40, 100))], [("p1", fw(-10, 50)), ("p2", fw(-40, 100))])
ok("z3:交叉检查定位拒收程序", len(problems) == 1 and problems[0]["program_id"] == "p1"
   and problems[0]["firmware_version"] == 1)

# ---------- 规范化契约内容（缓存键） ----------
base = {"type": "object",
        "properties": {"temp": {"type": "integer", "minimum": -10, "maximum": 50},
                       "site": {"type": "string", "enum": ["WQ-01", "WQ-02"]}},
        "required": ["temp", "site"], "additionalProperties": False}


def reordered(doc):
    """属性、required、枚举顺序不同，但语义完全相同。"""
    props = {k: (dict(v, enum=list(reversed(v["enum"]))) if "enum" in v else v)
             for k, v in reversed(list(doc["properties"].items()))}
    return {"additionalProperties": False, "required": list(reversed(doc["required"])),
            "properties": props, "type": "object"}


ok("规范化:语义相同哈希相同", canonical_hash(S(base)) == canonical_hash(S(reordered(base))))
ok("规范化:原始 JSON 不同", reordered(base) != base)
ok("规范化:语义不同哈希不同",
   canonical_hash(S(base)) != canonical_hash(S({**base, "required": ["temp"]})))
ok("规范化:枚举值不同哈希不同", canonical_hash(S({
    "type": "object", "properties": {"q": {"enum": ["N", "Q"]}},
    "additionalProperties": False})) != canonical_hash(S({
        "type": "object", "properties": {"q": {"enum": ["N", "Z"]}},
        "additionalProperties": False})))
ok("规范化:anyOf分支顺序无关", canonical_hash(S({"anyOf": [
    {"type": "object", "properties": {"a": {"type": "integer"}}, "additionalProperties": False},
    {"type": "object", "properties": {"b": {"type": "integer"}}, "additionalProperties": False}]}))
   == canonical_hash(S({"anyOf": [
       {"type": "object", "properties": {"b": {"type": "integer"}}, "additionalProperties": False},
       {"type": "object", "properties": {"a": {"type": "integer"}}, "additionalProperties": False}]})))
ok("规范化:anyOf重复分支去重", canonical_hash(S({"anyOf": [
    {"type": "object", "properties": {"a": {"type": "integer"}}, "additionalProperties": False}]}))
   == canonical_hash(S({"anyOf": [
       {"type": "object", "properties": {"a": {"type": "integer"}}, "additionalProperties": False},
       {"type": "object", "properties": {"a": {"type": "integer"}}, "additionalProperties": False}]})))
try:
    S({"anyOf": [{"type": "object", "properties": {"a": {"type": "integer"}},
                  "additionalProperties": False},
                 {"type": "object", "properties": {"a": {"type": "integer"}},
                  "additionalProperties": False}]})
    PASS.append("规范化:重复分支语法可接受")
except SchemaError:
    raise AssertionError("FAILED: 重复 anyOf 分支应当被语法接受")
ok("规范化:哈希为64位十六进制", len(canonical_hash(S(base))) == 64
   and all(c in "0123456789abcdef" for c in canonical_hash(S(base))))
ok("规范化:JSON 键有序", canonical_json(S(base)) == canonical_json(S(reordered(base))))

# ---------- VerdictCache：复用/重算计数与内容寻址 ----------
fw_a = S(base)
fw_b = S(reordered(base))
pg_c = S({"type": "object", "properties": {"temp": {"type": "integer", "minimum": -10,
                                                  "maximum": 50}},
          "required": ["temp"], "additionalProperties": False})
calls = {"n": 0}
real_find = find_counterexample


def counting_find(write, read):
    calls["n"] += 1
    return real_find(write, read)


import solver as solver_mod  # noqa: E402

solver_mod.find_counterexample = counting_find
try:
    cache = VerdictCache([(1, fw_a), (2, fw_b)], [("p", pg_c)], lambda fh, ph: {})
    problems = cache.check()
    ok("缓存:相同内容只求解一次", calls["n"] == 1)
    ok("缓存:重复组合计为复用", cache.recomputed == 1 and cache.reused == 1)
    ok("缓存:不兼容仍给出反例", len(problems) == 2)

    calls["n"] = 0
    cached_cex = real_find(fw_a, pg_c)
    ok("缓存:前置反例存在", cached_cex is not None)
    calls["n"] = 0
    seen = {(canonical_hash(fw_a), canonical_hash(pg_c)): cached_cex}
    cache2 = VerdictCache([(1, fw_a)], [("p", pg_c)], lambda fh, ph: seen)
    problems2 = cache2.check()
    ok("缓存:已有结论不再调用 z3", calls["n"] == 0 and cache2.reused == 1
       and cache2.recomputed == 0)
    ok("缓存:命中反例照常报问题", len(problems2) == 1
       and problems2[0]["counterexample"] == cached_cex)

    bad = {(canonical_hash(fw_a), canonical_hash(pg_c)): {"temp": 999, "site": "WQ-01"}}
    calls["n"] = 0
    cache3 = VerdictCache([(1, fw_a)], [("p", pg_c)], lambda fh, ph: bad)
    cache3.check()
    ok("缓存:失效反例复核不过则重算", calls["n"] == 1 and cache3.recomputed == 1)
finally:
    solver_mod.find_counterexample = real_find

print(f"全部 {len(PASS)} 项单元自测通过")
