"""发布事务逻辑：校验 -> 加锁 -> 全量兼容检查 -> 落库，失败整体回滚。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import select, update

from db import (CompatibilityVerdict, DeviceModel, FirmwareFormat, ProgramFormat,
                PublishLog, SessionLocal)
from schema_spec import SchemaError, parse_schema
from solver import VerdictCache

MAX_FORMATS = 8  # 每型号固件格式、程序格式各至多 8 份
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


@dataclass
class PublishOutcome:
    ok: bool
    message: str
    problems: list = field(default_factory=list)
    version: Optional[int] = None
    reused: int = 0       # 本次复用已有兼容结论的组合数
    recomputed: int = 0   # 本次实际调用 z3 重算的组合数

    @property
    def checked_pairs(self) -> int:
        return self.reused + self.recomputed


def valid_name(name: str) -> bool:
    return bool(NAME_RE.match(name or ""))


def _lock_model(s, model_id: int):
    """按型号串行化发布：PostgreSQL 用 SELECT ... FOR UPDATE 行锁；
    SQLite 用一次空 UPDATE 提前获取写锁（配合 busy_timeout 等待）。
    必须在读取状态之前调用，保证后续读到的都是最新已提交状态，
    并发升级不会各自通过后留下不兼容组合。"""
    if s.bind.dialect.name == "postgresql":
        s.execute(select(DeviceModel.id).where(DeviceModel.id == model_id)
                  .with_for_update()).one()
    else:
        s.execute(update(DeviceModel).where(DeviceModel.id == model_id)
                  .values(name=DeviceModel.name))


def _log(model_id: int, action: str, program_id, doc, result: str, detail):
    with SessionLocal() as s:
        s.add(PublishLog(model_id=model_id, action=action, program_id=program_id,
                         doc=doc, result=result, detail=detail))
        s.commit()


def _load_state(s, model_id: int):
    firmwares = s.execute(
        select(FirmwareFormat).where(FirmwareFormat.model_id == model_id)
        .order_by(FirmwareFormat.version)).scalars().all()
    programs = s.execute(
        select(ProgramFormat).where(ProgramFormat.model_id == model_id)
        .order_by(ProgramFormat.program_id)).scalars().all()
    return firmwares, programs


def _verdict_loader(s):
    """从当前事务读取按规范化契约哈希寻址的已有结论。

    返回 {(fw_hash, prog_hash): counterexample_or_None}；键存在即命中
    （值为 None 表示已证明兼容）。
    """
    def load(fw_hashes, prog_hashes):
        fw = set(fw_hashes)
        pg = set(prog_hashes)
        if not fw or not pg:
            return {}
        rows = s.execute(
            select(CompatibilityVerdict)
            .where(CompatibilityVerdict.firmware_hash.in_(fw),
                   CompatibilityVerdict.program_hash.in_(pg))).scalars().all()
        return {(r.firmware_hash, r.program_hash):
                (None if r.compatible else r.counterexample) for r in rows}
    return load


def _store_verdicts(s, new_verdicts):
    """把本次新求得的结论写入当前发布事务（提交/回滚与格式变更保持一致）。

    new_verdicts: [(fw_hash, prog_hash, counterexample_or_None)]
    使用 ON CONFLICT DO NOTHING：不同型号并发算出同一组合时不会互相打断，
    已有的内容寻址结论也不会被覆盖。
    """
    if not new_verdicts:
        return
    rows = [{"firmware_hash": fh, "program_hash": ph,
             "compatible": cex is None, "counterexample": cex}
            for fh, ph, cex in new_verdicts]
    dialect = s.bind.dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as dialect_insert
    else:
        dialect_insert = None
    if dialect_insert is None:  # 其他方言：逐条插入（并发冲突交给唯一约束/上层重试）
        s.add_all(CompatibilityVerdict(**row) for row in rows)
        return
    stmt = dialect_insert(CompatibilityVerdict).values(rows)
    s.execute(stmt.on_conflict_do_nothing(
        index_elements=["firmware_hash", "program_hash"]))


def _cache_detail(cache) -> dict:
    return {"checked_pairs": cache.reused + cache.recomputed,
            "reused": cache.reused, "recomputed": cache.recomputed}


def publish_firmware(model_id: int, raw_doc) -> PublishOutcome:
    """追加一份固件写入格式（只追加）。"""
    try:
        schema = parse_schema(raw_doc)
    except SchemaError as e:
        _log(model_id, "firmware", None, raw_doc, "invalid", {"error": str(e)})
        return PublishOutcome(False, f"格式不符合受限语法：{e}")

    with SessionLocal() as s:
        model = s.get(DeviceModel, model_id)
        if model is None:
            return PublishOutcome(False, "型号不存在")
        _lock_model(s, model_id)
        firmwares, programs = _load_state(s, model_id)
        if len(firmwares) >= MAX_FORMATS:
            s.rollback()
            msg = f"固件格式已达 {MAX_FORMATS} 份上限，无法继续追加"
            _log(model_id, "firmware", None, raw_doc, "invalid", {"error": msg})
            return PublishOutcome(False, msg)

        new_version = (firmwares[-1].version if firmwares else 0) + 1
        fw_pairs = [(f.version, parse_schema(f.doc)) for f in firmwares]
        fw_pairs.append((new_version, schema))
        prog_pairs = [(p.program_id, parse_schema(p.doc)) for p in programs]

        cache = VerdictCache(fw_pairs, prog_pairs, _verdict_loader(s))
        problems = cache.check()
        detail = _cache_detail(cache)
        if problems:
            # 拒绝：不写任何格式行，保持已发布状态不变。本次在锁内基于已提交
            # 状态新求得的结论（含反例）与拒绝日志在同一事务提交：要么一起
            # 生效，要么一起不生效，绝不会出现半写入的缓存。
            _store_verdicts(s, cache.new_verdicts())
            s.add(PublishLog(model_id=model_id, action="firmware", program_id=None,
                             doc=raw_doc, result="rejected",
                             detail={"problems": problems, **detail}))
            s.commit()
            return PublishOutcome(False, "存在不兼容组合，已拒绝发布（已发布格式未变）",
                                  problems, reused=cache.reused,
                                  recomputed=cache.recomputed)

        _store_verdicts(s, cache.new_verdicts())
        s.add(FirmwareFormat(model_id=model_id, version=new_version, doc=raw_doc))
        s.add(PublishLog(model_id=model_id, action="firmware", program_id=None,
                         doc=raw_doc, result="published",
                         detail={"version": new_version, **detail}))
        s.commit()
        return PublishOutcome(True, f"固件写入格式 v{new_version} 已发布",
                              version=new_version, reused=cache.reused,
                              recomputed=cache.recomputed)


def publish_program(model_id: int, program_id: str, raw_doc) -> PublishOutcome:
    """按程序号发布/替换程序读取格式。"""
    if not valid_name(program_id):
        return PublishOutcome(False, "程序号只能由字母数字及 _ . - 组成，长度 1~64")
    try:
        schema = parse_schema(raw_doc)
    except SchemaError as e:
        _log(model_id, "program", program_id, raw_doc, "invalid", {"error": str(e)})
        return PublishOutcome(False, f"格式不符合受限语法：{e}")

    with SessionLocal() as s:
        model = s.get(DeviceModel, model_id)
        if model is None:
            return PublishOutcome(False, "型号不存在")
        _lock_model(s, model_id)
        firmwares, programs = _load_state(s, model_id)
        existing = {p.program_id: p for p in programs}
        if program_id not in existing and len(programs) >= MAX_FORMATS:
            s.rollback()
            msg = f"程序格式已达 {MAX_FORMATS} 个上限，新程序号无法登记"
            _log(model_id, "program", program_id, raw_doc, "invalid", {"error": msg})
            return PublishOutcome(False, msg)

        fw_pairs = [(f.version, parse_schema(f.doc)) for f in firmwares]
        prog_pairs = [(p.program_id, parse_schema(p.doc)) for p in programs
                      if p.program_id != program_id]
        prog_pairs.append((program_id, schema))

        cache = VerdictCache(fw_pairs, prog_pairs, _verdict_loader(s))
        problems = cache.check()
        detail = _cache_detail(cache)
        if problems:
            # 拒绝：不写任何格式行；新结论（含反例）与拒绝日志同一事务提交。
            _store_verdicts(s, cache.new_verdicts())
            s.add(PublishLog(model_id=model_id, action="program", program_id=program_id,
                             doc=raw_doc, result="rejected",
                             detail={"problems": problems, **detail}))
            s.commit()
            return PublishOutcome(False, "存在不兼容组合，已拒绝发布（已发布格式未变）",
                                  problems, reused=cache.reused,
                                  recomputed=cache.recomputed)

        _store_verdicts(s, cache.new_verdicts())
        if program_id in existing:
            existing[program_id].doc = raw_doc
        else:
            s.add(ProgramFormat(model_id=model_id, program_id=program_id, doc=raw_doc))
        s.add(PublishLog(model_id=model_id, action="program", program_id=program_id,
                         doc=raw_doc, result="published", detail=detail))
        s.commit()
        return PublishOutcome(True, f"程序「{program_id}」读取格式已发布",
                              reused=cache.reused, recomputed=cache.recomputed)
