"""用 z3 全量判定：某固件写入格式允许的全部报文是否都被某程序读取格式接受。

判定 L(write) ⊆ L(read)：向求解器查询「满足 write 但不满足 read」的报文。
- UNSAT  => 不存在反例，兼容（这是对全部报文的证明，不是抽样）。
- SAT    => 取模型还原出一个合法反例报文（满足 write、被 read 拒收）。

报文编码：对每个字段建立 4 个变量 —— 是否出现 present、值类型 kind
(0=null, 1=integer, 2=string)、整数值 ival、字符串值在字段字符串表中的下标 sval。
"""
from __future__ import annotations

from z3 import And, Bool, Implies, Int, Not, Or, Solver, is_true, sat

from schema_spec import Schema, canonical_hash, message_valid

KIND_NULL, KIND_INT, KIND_STR = 0, 1, 2
SOLVER_TIMEOUT_MS = 15000


class _Encoder:
    def __init__(self, fields, str_tables):
        self.fields = fields
        self.str_tables = str_tables  # field -> sorted list[str]
        self.present, self.kind, self.ival, self.sval = {}, {}, {}, {}
        for i, f in enumerate(fields):
            self.present[f] = Bool(f"p{i}")
            self.kind[f] = Int(f"k{i}")
            self.ival[f] = Int(f"v{i}")
            self.sval[f] = Int(f"s{i}")

    def wellformed(self):
        cons = []
        for f in self.fields:
            p, k, v, s = self.present[f], self.kind[f], self.ival[f], self.sval[f]
            tbl = self.str_tables.get(f) or []
            cons.append(Or(k == KIND_NULL, k == KIND_INT, k == KIND_STR))
            # 缺省字段取确定默认值，保证反例报文干净
            cons.append(Implies(Not(p), And(k == KIND_NULL, v == 0, s == 0)))
            cons.append(And(s >= 0, s < len(tbl)) if tbl else (s == 0))
        return cons

    def field_ok(self, f, spec):
        k, v, s = self.kind[f], self.ival[f], self.sval[f]
        opts = []
        if spec.allow_null:
            opts.append(k == KIND_NULL)
        if spec.int_kind == "range":
            c = [k == KIND_INT]
            if spec.int_lo is not None:
                c.append(v >= spec.int_lo)
            if spec.int_hi is not None:
                c.append(v <= spec.int_hi)
            opts.append(And(*c))
        elif spec.int_kind == "set":
            opts.append(And(k == KIND_INT, Or(*[v == x for x in sorted(spec.int_set)])))
        if spec.str_set:
            tbl = self.str_tables[f]
            opts.append(And(k == KIND_STR, Or(*[s == tbl.index(x) for x in sorted(spec.str_set)])))
        return Or(*opts)

    def branch_ok(self, branch):
        cons = []
        props = branch.properties
        for f in self.fields:
            if f in props:
                c = self.field_ok(f, props[f])
                if f in branch.required:
                    cons.append(And(self.present[f], c))
                else:
                    cons.append(Implies(self.present[f], c))
            else:
                # additionalProperties: false —— 未声明字段不得出现
                cons.append(Not(self.present[f]))
        return And(*cons)

    def schema_ok(self, schema: Schema):
        return Or(*[self.branch_ok(b) for b in schema.branches])


def find_counterexample(write: Schema, read: Schema):
    """若存在被 read 拒收的 write 合法报文，返回该报文 dict；否则返回 None。"""
    names = set()
    for sch in (write, read):
        for b in sch.branches:
            names |= set(b.properties)
    fields = sorted(names)
    str_tables = {}
    for f in fields:
        strs = set()
        for sch in (write, read):
            for b in sch.branches:
                spec = b.properties.get(f)
                if spec and spec.str_set:
                    strs |= set(spec.str_set)
        if strs:
            str_tables[f] = sorted(strs)

    enc = _Encoder(fields, str_tables)
    s = Solver()
    s.set("timeout", SOLVER_TIMEOUT_MS)
    s.add(enc.wellformed())
    s.add(enc.schema_ok(write))
    s.add(Not(enc.schema_ok(read)))
    r = s.check()
    if r != sat:
        if str(r) == "unknown":
            raise RuntimeError("z3 求解超时/未知，无法完成全量判定")
        return None

    m = s.model()
    msg = {}
    for f in fields:
        if is_true(m.eval(enc.present[f], model_completion=True)):
            k = m.eval(enc.kind[f], model_completion=True).as_long()
            if k == KIND_NULL:
                msg[f] = None
            elif k == KIND_INT:
                msg[f] = m.eval(enc.ival[f], model_completion=True).as_long()
            else:
                msg[f] = str_tables[f][m.eval(enc.sval[f], model_completion=True).as_long()]
    # 复核：反例必须确实满足写入格式、且确实被读取格式拒收
    if not message_valid(msg, write) or message_valid(msg, read):
        raise RuntimeError(f"内部错误：z3 反例复核失败 {msg!r}")
    return msg


def check_compatibility(fw_pairs, prog_pairs):
    """全量交叉检查：每种固件写入格式 × 每个现用程序。

    fw_pairs: [(version, Schema)]，prog_pairs: [(program_id, Schema)]
    返回问题列表 [{firmware_version, program_id, counterexample}]，空列表表示全部兼容。
    """
    problems = []
    for fv, fw in fw_pairs:
        for pid, prog in prog_pairs:
            cex = find_counterexample(fw, prog)
            if cex is not None:
                problems.append({
                    "firmware_version": fv,
                    "program_id": pid,
                    "counterexample": cex,
                })
    return problems


class VerdictCache:
    """一次发布事务内的兼容结论缓存。

    以「固件规范化哈希 × 程序规范化哈希」为键。fw_pairs / prog_pairs 是本次
    发布将要生效的全部组合（版本号、程序号在调用前已确定）。已有结论直接复用，
    未命中的组合才调用 find_counterexample 求解，新结论记录在内存中，由调用方
    随发布事务一起落库。
    """

    def __init__(self, fw_pairs, prog_pairs, loader):
        self.fw_pairs = fw_pairs
        self.prog_pairs = prog_pairs
        self.reused = 0
        self.recomputed = 0
        self._fw_hashes = [canonical_hash(s) for _, s in fw_pairs]
        self._prog_hashes = [canonical_hash(s) for _, s in prog_pairs]
        self._verdicts = loader(self._fw_hashes, self._prog_hashes)
        self._new_verdicts = {}

    def check(self):
        problems = []
        for i, (fv, fw) in enumerate(self.fw_pairs):
            for j, (pid, prog) in enumerate(self.prog_pairs):
                key = (self._fw_hashes[i], self._prog_hashes[j])
                if key in self._new_verdicts:
                    verdict = self._new_verdicts[key]
                    self.reused += 1
                elif key in self._verdicts:
                    verdict = self._verdicts[key]
                    # 防御：缓存的反例必须仍能复核通过，否则当作未命中重算
                    if verdict is not None and not (
                            message_valid(verdict, fw) and not message_valid(verdict, prog)):
                        del self._verdicts[key]
                        verdict = self._solve_and_remember(key, fw, prog)
                    else:
                        self.reused += 1
                else:
                    verdict = self._solve_and_remember(key, fw, prog)
                if verdict is not None:
                    problems.append({
                        "firmware_version": fv,
                        "program_id": pid,
                        "counterexample": verdict,
                    })
        return problems

    def _solve_and_remember(self, key, fw, prog):
        cex = find_counterexample(fw, prog)
        self._new_verdicts[key] = cex
        self.recomputed += 1
        return cex

    def new_verdicts(self):
        """本次新求得、需要随事务落库的结论：[(fw_hash, prog_hash, cex)]。

        按哈希排序，保证并发事务以相同顺序获取唯一索引锁。
        """
        return sorted(((fh, ph, cex) for (fh, ph), cex in self._new_verdicts.items()),
                      key=lambda item: (item[0], item[1]))
