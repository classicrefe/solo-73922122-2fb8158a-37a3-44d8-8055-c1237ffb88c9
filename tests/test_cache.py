"""兼容结论缓存专项集成自测（自带服务进程，默认使用临时 SQLite）。

覆盖：
1. 冷启动发布：新增固件 / 新增程序只重算新出现的组合；
2. 内容寻址：相同契约内容（改写字段/枚举顺序）命中缓存；
3. 替换一侧：只重算受影响组合；
4. 服务重启：已有结论全部复用；
5. 拒绝：已发布状态不变，拒绝依据与计数正确；相同内容重试全部复用；
6. 拒绝不泄露过期结论：旧内容的反例不会套用到新内容上；
7. 并发发布：恰好一方成功、最终状态全量兼容，且缓存结论与新计算一致。

运行：python3 tests/test_cache.py
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import httpx

APP_DIR = os.path.join(os.path.dirname(__file__), "..", "app")
sys.path.insert(0, APP_DIR)
from schema_spec import message_valid, parse_schema  # noqa: E402
from solver import check_compatibility  # noqa: E402

PASS = []


def ok(name, cond, extra=""):
    assert cond, f"FAILED: {name} {extra}"
    PASS.append(name)
    print(f"  ✔ {name}")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def temp_doc(lo, hi, qc=None, qc_required=False, order=None):
    props = {"temp": {"type": "integer", "minimum": lo, "maximum": hi},
             "site": {"type": "string", "enum": ["WQ-01", "WQ-02"]}}
    required = ["temp", "site"]
    if qc is not None:
        props["qc"] = {"enum": qc}
        if qc_required:
            required.append("qc")
    if order == "reversed":
        props = dict(reversed(list(props.items())))
        required = list(reversed(required))
    return {"type": "object", "properties": props, "required": required,
            "additionalProperties": False}


RUNNER = r"""
import sys, threading
sys.path.insert(0, sys.argv[1])
import uvicorn

server = uvicorn.Server(uvicorn.Config("main:app", host="127.0.0.1",
                                       port=int(sys.argv[2]), log_level="warning"))
# 父进程关闭 stdin（EOF）时优雅退出，便于自测模拟真实重启
def _watch():
    sys.stdin.buffer.read()
    server.should_exit = True
threading.Thread(target=_watch, daemon=True).start()
server.run()
"""


class Server:
    def __init__(self, db_path):
        self.db_path = db_path
        self.proc = None
        self.base = None

    def start(self):
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        env = dict(os.environ)
        env["DATABASE_URL"] = f"sqlite:///{self.db_path}"
        self.log = open(os.path.join(os.path.dirname(self.db_path),
                                     f"server-{self.port}.log"), "ab")
        self.proc = subprocess.Popen(
            [sys.executable, "-c", RUNNER, APP_DIR, str(self.port)],
            cwd=APP_DIR, env=env, stdin=subprocess.PIPE,
            stdout=self.log, stderr=self.log)
        client = httpx.Client(base_url=self.base, timeout=2)
        for _ in range(100):
            try:
                if client.get("/api/health").status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        raise RuntimeError("服务未能启动")

    def stop(self):
        if self.proc is not None:
            self.proc.stdin.close()  # 触发子进程优雅退出（模拟真实重启）
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None
            self.log.close()


def counts(r):
    b = r.json()
    return b.get("reused"), b.get("recomputed"), b.get("checked_pairs")


def main():
    tmp = tempfile.mkdtemp(prefix="cache-test-")
    db_path = os.path.join(tmp, "contracts.db")
    srv = Server(db_path)
    srv.start()
    c = httpx.Client(base_url=srv.base, timeout=30)

    model = f"cache{uuid.uuid4().hex[:8]}"
    assert c.post("/api/models", json={"name": model}).status_code == 201

    def fw(doc):
        return c.post(f"/api/models/{model}/firmware", json={"schema": doc})

    def pg(pid, doc):
        return c.post(f"/api/models/{model}/programs",
                      json={"program_id": pid, "schema": doc})

    print("1) 冷启动：只重算新出现的组合")
    r = fw(temp_doc(0, 50))
    ok("首份固件发布（尚无程序，0 组合）", r.status_code == 200 and counts(r) == (0, 0, 0), r.text)
    r = pg("p1", temp_doc(0, 100))
    ok("新增程序 p1：1 个新组合", r.status_code == 200 and counts(r) == (0, 1, 1), r.text)
    r = pg("p2", temp_doc(0, 60))
    ok("新增程序 p2：复用 1 / 重算 1", r.status_code == 200 and counts(r) == (1, 1, 2), r.text)

    print("2) 新增固件：只重算新固件 × 现用程序")
    r = fw(temp_doc(0, 40))
    ok("新增固件 v2：复用 2 / 重算 2", r.status_code == 200 and counts(r) == (2, 2, 4), r.text)

    print("3) 替换程序：只重算该程序相关组合")
    r = pg("p2", temp_doc(0, 90))
    ok("替换 p2：复用 2 / 重算 2", r.status_code == 200 and counts(r) == (2, 2, 4), r.text)
    r = pg("p2", temp_doc(0, 90))
    ok("替换为相同内容：全部复用", r.status_code == 200 and counts(r) == (4, 0, 4), r.text)

    print("4) 规范化内容寻址：顺序不同仍命中")
    r = pg("p2", temp_doc(0, 90, order="reversed"))
    ok("字段/required 顺序不同视为同一契约", r.status_code == 200 and counts(r) == (4, 0, 4), r.text)
    r = pg("p2", temp_doc(0, 90, qc=None))
    ok("恢复原写法仍命中", r.status_code == 200 and counts(r) == (4, 0, 4), r.text)

    print("5) 服务重启后仍可复用")
    srv.stop()
    srv.start()
    c = httpx.Client(base_url=srv.base, timeout=30)
    r = fw(temp_doc(0, 40))  # 与 v2 内容相同，新增 v3
    ok("重启后新增同内容固件：6 个组合全部复用",
       r.status_code == 200 and counts(r) == (6, 0, 6), r.text)
    r = pg("p2", temp_doc(0, 70))  # 新内容
    ok("重启后替换程序：复用旧组合、重算受影响组合",
       r.status_code == 200 and counts(r) == (4, 2, 6), r.text)
    # 当前状态：固件 v1(0~50) v2(0~40) v3(0~40)；程序 p1(0~100) p2(0~70)

    st = c.get(f"/api/models/{model}").json()
    ok("重启后格式数据保留", len(st["firmwares"]) == 3 and len(st["programs"]) == 2)

    print("6) 拒绝：状态不变、依据与计数正确、重试全部复用")
    bad = temp_doc(0, 80)  # p2(0~70) 会拒收，p1(0~100) 接受
    r = fw(bad)
    b = r.json()
    reused, recomputed, checked = counts(r)
    ok("不兼容固件被拒绝", r.status_code == 409 and not b["ok"], r.text)
    ok("拒绝依据含拒收程序与反例", b["problems"][0]["program_id"] == "p2"
       and message_valid(b["problems"][0]["counterexample"], parse_schema(bad))
       and not message_valid(b["problems"][0]["counterexample"], parse_schema(temp_doc(0, 70))),
       json.dumps(b, ensure_ascii=False))
    ok("拒绝计数：旧 6 组合复用、新 2 组合重算", (reused, recomputed, checked) == (6, 2, 8))
    st2 = c.get(f"/api/models/{model}").json()
    ok("拒绝后已发布格式不变", len(st2["firmwares"]) == 3 and len(st2["programs"]) == 2)
    cex1 = b["problems"][0]["counterexample"]

    r = fw(bad)
    ok("相同内容重试：全部复用（含反例）", r.status_code == 409 and counts(r) == (8, 0, 8), r.text)
    ok("重试返回一致反例", r.json()["problems"][0]["counterexample"] == cex1)

    print("7) 拒绝结论不会套用到新内容上")
    # p2 放宽与 p1 内容相同（0~100）。此时 4 份固件 × p1/p2 全部命中缓存。
    r = pg("p2", temp_doc(0, 100))
    ok("程序放宽后发布成功", r.status_code == 200, r.text)
    ok("内容与 p1 相同时，全部组合命中", counts(r) == (6, 0, 6), r.text)
    # 新固件 0~90：旧 p2(0~70) 会拒收它，但现用 p2 是 0~100；
    # 若把旧反例当作新内容结论，就会误拒。必须重算出兼容。
    r = fw(temp_doc(0, 90))
    ok("新内容不相干的反例未被误用", r.status_code == 200 and counts(r) == (7, 1, 8), r.text)
    # 对照：完全相同的一份固件重复追加，全部复用
    r = fw(temp_doc(0, 90))
    ok("相同固件重复追加：全部复用", r.status_code == 200 and counts(r) == (10, 0, 10), r.text)
    print("8) 并发发布：无过期结论、最终状态全量兼容")

    def reset_model():
        name = f"conc{uuid.uuid4().hex[:8]}"
        assert c.post("/api/models", json={"name": name}).status_code == 201
        assert c.post(f"/api/models/{name}/firmware",
                      json={"schema": temp_doc(0, 50)}).status_code == 200
        assert c.post(f"/api/models/{name}/programs",
                      json={"program_id": "P", "schema": temp_doc(0, 100)}).status_code == 200
        return name

    for round_no in range(5):
        cur = reset_model()
        results = {}
        barrier = threading.Barrier(2)

        def narrow():
            barrier.wait()
            results["A"] = c.post(f"/api/models/{cur}/programs",
                                  json={"program_id": "P", "schema": temp_doc(0, 50)})

        def widen():
            barrier.wait()
            results["B"] = c.post(f"/api/models/{cur}/firmware",
                                  json={"schema": temp_doc(0, 100)})

        t1 = threading.Thread(target=narrow)
        t2 = threading.Thread(target=widen)
        t1.start(); t2.start(); t1.join(); t2.join()
        ok(f"第{round_no+1}轮并发恰好一方成功",
           sorted(r.status_code for r in results.values()) == [200, 409],
           f"{ {k: v.status_code for k, v in results.items()} }")
        st = c.get(f"/api/models/{cur}").json()
        fw_pairs = [(f["version"], parse_schema(f["schema"])) for f in st["firmwares"]]
        pg_pairs = [(p["program_id"], parse_schema(p["schema"])) for p in st["programs"]]
        ok(f"第{round_no+1}轮并发后状态全量兼容",
           check_compatibility(fw_pairs, pg_pairs) == [])
        # 以相同内容重新发布现用程序：缓存必须与当前状态一致，全部复用
        r = c.post(f"/api/models/{cur}/programs",
                   json={"program_id": "P", "schema": st["programs"][0]["schema"]})
        ok(f"第{round_no+1}轮并发后缓存结论与状态一致（全部复用）",
           r.status_code == 200 and counts(r) == (len(fw_pairs), 0, len(fw_pairs)), r.text)

    print("9) 网页展示复用与重算数量")
    html = c.get(f"/models/{model}").text
    ok("型号页含复用/重算计数", "复用" in html and "重算" in html)
    html = c.get("/").text
    ok("首页显示已保存结论数", "已保存" in html and "条结论" in html)

    print("10) 缓存统计可查询")
    stats = c.get("/api/cache/stats").json()
    ok("统计含兼容/不兼容计数", stats["verdicts"] == stats["compatible"] + stats["incompatible"]
       and stats["verdicts"] > 0, json.dumps(stats))

    srv.stop()
    print(f"\n全部 {len(PASS)} 项缓存专项自测通过")
    print(f"WORKDIR={tmp}")
    print(f"MODEL={model}")
    return tmp, model


if __name__ == "__main__":
    main()
