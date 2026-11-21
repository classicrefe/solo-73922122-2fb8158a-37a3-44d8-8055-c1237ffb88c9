"""发布事务原子性白盒自测：缓存结论与发布结果必须同生共死。

覆盖：
1. 成功发布：格式行、发布日志、新兼容结论在同一事务提交；
2. 提交失败：三者全部不落库（不泄露半写入的缓存）；
3. 拒绝发布：已发布格式不变，但拒绝日志与新结论一起提交（反例可复用）；
4. 结论写入失败：整体回滚，格式与日志均不落库；
5. 结论以规范化内容寻址：同一内容换顺序/换型号可复用。

运行：python3 tests/test_transactions.py
"""
import os
import sys
import tempfile
import uuid

TMP = tempfile.mkdtemp(prefix="tx-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(TMP, 'contracts.db')}"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import db  # noqa: E402
import service  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

PASS = []


def ok(name, cond, extra=""):
    assert cond, f"FAILED: {name} {extra}"
    PASS.append(name)
    print(f"  ✔ {name}")


db.Base.metadata.create_all(db.engine)


def doc(lo, hi):
    return {"type": "object",
            "properties": {"temp": {"type": "integer", "minimum": lo, "maximum": hi}},
            "required": ["temp"], "additionalProperties": False}


def new_model():
    with db.SessionLocal() as s:
        m = db.DeviceModel(name=f"m{uuid.uuid4().hex[:8]}")
        s.add(m)
        s.commit()
        return m.id


def state(model_id):
    with db.SessionLocal() as s:
        fw = s.execute(select(db.FirmwareFormat)
                       .where(db.FirmwareFormat.model_id == model_id)).scalars().all()
        pg = s.execute(select(db.ProgramFormat)
                       .where(db.ProgramFormat.model_id == model_id)).scalars().all()
        logs = s.execute(select(db.PublishLog)
                         .where(db.PublishLog.model_id == model_id)).scalars().all()
        verdicts = s.execute(select(func.count(db.CompatibilityVerdict.id))).scalar_one()
        return {"fw": len(fw), "pg": len(pg), "logs": len(logs), "verdicts": verdicts}


class FailingCommitSession(Session):
    """在 commit 时抛错，模拟提交阶段故障。"""
    def commit(self):
        raise RuntimeError("模拟提交失败")


class FailingInsertSession(Session):
    """在写入结论时抛错，模拟存储层故障。"""
    pass


original_sessionlocal = service.SessionLocal


def main():
    print("1) 成功发布：格式 + 日志 + 新结论同事务提交")
    m = new_model()
    out = service.publish_firmware(m, doc(0, 50))
    st = state(m)
    ok("固件发布成功", out.ok)
    ok("成功后有格式行与日志（尚无程序，0 组合）",
       st == {"fw": 1, "pg": 0, "logs": 1, "verdicts": 0}, json(st))
    before = state(m)
    out = service.publish_program(m, "p", doc(0, 100))
    after = state(m)
    ok("程序发布新增 1 条结论", out.ok and after["verdicts"] == before["verdicts"] + 1)
    ok("程序发布新增程序行/日志", after["pg"] == 1 and after["logs"] == before["logs"] + 1)

    print("2) 提交失败时不得泄露缓存或格式")
    m2 = new_model()
    factory = service.SessionLocal

    def failing_factory():
        return FailingCommitSession(bind=db.engine, expire_on_commit=False)

    service.SessionLocal = failing_factory
    try:
        raised = False
        try:
            service.publish_firmware(m2, doc(0, 50))
        except RuntimeError as e:
            raised = "模拟提交失败" in str(e)
        ok("提交失败向外抛出", raised)
    finally:
        service.SessionLocal = factory
    st = state(m2)
    ok("提交失败后无格式/日志/结论残留", st["fw"] == 0 and st["logs"] == 0, json(st))
    # 全局结论数不应因失败事务增加
    ok("失败事务未新增任何结论", st["verdicts"] == after["verdicts"], json(st))

    print("3) 拒绝发布：格式不变，拒绝日志与新结论同事务提交")
    m3 = new_model()
    assert service.publish_firmware(m3, doc(0, 100)).ok
    assert service.publish_program(m3, "p", doc(0, 200)).ok
    before = state(m3)
    out = service.publish_firmware(m3, doc(0, 300))
    ok("不兼容固件被拒绝", not out.ok and len(out.problems) == 1)
    st = state(m3)
    ok("拒绝后已发布格式不变", st["fw"] == before["fw"] and st["pg"] == before["pg"])
    ok("拒绝后写入了拒绝日志", st["logs"] == before["logs"] + 1)
    ok("拒绝的新结论已持久化（反例可复用）", st["verdicts"] > before["verdicts"], json(st))
    ok("拒绝计数 reused/recomputed 之和等于组合数",
       out.reused + out.recomputed == out.checked_pairs == 2)

    out2 = service.publish_firmware(m3, doc(0, 300))
    ok("相同内容重试：反例全部复用", not out2.ok and out2.recomputed == 0
       and out2.reused == 2 and out2.problems == out.problems)

    print("3b) 拒绝路径提交失败：不泄露缓存/日志，后续仍可正常判定")
    m3b = new_model()
    assert service.publish_firmware(m3b, doc(0, 100)).ok
    assert service.publish_program(m3b, "p", doc(0, 200)).ok
    before = state(m3b)

    def failing_factory_2():
        return FailingCommitSession(bind=db.engine, expire_on_commit=False)

    service.SessionLocal = failing_factory_2
    try:
        raised = False
        try:
            service.publish_firmware(m3b, doc(0, 300))
        except RuntimeError as e:
            raised = "模拟提交失败" in str(e)
        ok("拒绝路径提交失败向外抛出", raised)
    finally:
        service.SessionLocal = factory
    st = state(m3b)
    ok("拒绝路径失败后无日志/结论残留",
       st["logs"] == before["logs"] and st["verdicts"] == before["verdicts"], json(st))
    out = service.publish_firmware(m3b, doc(0, 300))
    ok("失败后重试仍能正确拒绝并给出反例",
       not out.ok and out.recomputed == 0 and out.reused == 2
       and out.checked_pairs == 2 and out.problems[0]["counterexample"] is not None,
       json(out.problems))

    print("3c) 并发发布相同内容：结论不冲突")
    import threading
    models = [new_model() for _ in range(4)]
    for mid in models:
        assert service.publish_firmware(mid, doc(0, 100)).ok
    results = {}

    def publish(mid):
        results[mid] = service.publish_program(mid, "p", doc(-5, 205))

    threads = [threading.Thread(target=publish, args=(mid,)) for mid in models]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ok("并发发布相同程序内容全部成功", all(r.ok for r in results.values()),
       json({k: (v.ok, v.message) for k, v in results.items()}))
    ok("并发后至少一方重算，不出现冲突",
       sum(r.recomputed for r in results.values()) >= 1,
       json({k: (v.reused, v.recomputed) for k, v in results.items()}))

    print("4) 结论写入失败：整体回滚")
    m4 = new_model()
    assert service.publish_firmware(m4, doc(0, 100)).ok
    before = state(m4)

    real_store = service._store_verdicts

    def boom(s, rows):
        raise RuntimeError("模拟结论写入失败")

    service._store_verdicts = boom
    try:
        raised = False
        try:
            service.publish_program(m4, "p", doc(0, 200))
        except RuntimeError as e:
            raised = "模拟结论写入失败" in str(e)
        ok("结论写入失败向外抛出", raised)
    finally:
        service._store_verdicts = real_store
    st = state(m4)
    ok("结论写入失败后程序/日志未落库",
       st["pg"] == 0 and st["logs"] == before["logs"], json(st))
    ok("结论写入失败后无新结论", st["verdicts"] == before["verdicts"], json(st))
    ok("之后仍可正常发布", service.publish_program(m4, "p", doc(0, 200)).ok)

    print("5) 内容寻址跨型号复用")
    m5 = new_model()
    out = service.publish_firmware(m5, doc(0, 100))
    ok("另一个型号的同内容固件无需重算", out.ok and out.recomputed == 0 and out.reused == 0)
    out = service.publish_program(m5, "p", doc(0, 200))
    ok("另一个型号的同内容程序复用结论", out.ok and out.reused == 1 and out.recomputed == 0)

    print(f"\n全部 {len(PASS)} 项事务原子性自测通过")


def json(d):
    import json as _json
    return _json.dumps(d, ensure_ascii=False)


if __name__ == "__main__":
    main()
