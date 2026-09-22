"""三个线上问题的修复验证（真实 HTTP：起 uvicorn 子进程 + httpx 并发冲击）。

覆盖：
1. 新建应用重复提交 —— 同一幂等键重试/并发只落一条；同键不同载荷 409；
   无键同名并发 N 个请求只落一条、其余 409（不再 500、不再双卡）。
2. 资产作战台缓存 —— 状态流转/新建提交后，下一次汇总立即反映，不等 TTL。
3. 负责人空缺的应用 —— 其他业务线账号读/写一律 403（带原因）；
   管理员、本业务线负责人、被授权账号的正常能力不受影响。
4. 存量台账与留痕一条不动 —— 越权尝试前后，目标应用行与留痕数完全一致。

运行：python3 verify_fixes.py   （需要 fastapi/uvicorn/httpx）
"""
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(tmp, "test.db")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import httpx  # noqa: E402

PORT = int(os.environ.get("VERIFY_PORT", "8123"))
BASE = f"http://127.0.0.1:{PORT}"

failures = []


def check(name, cond, detail=""):
    print(("  ✓ " if cond else "  ✗ ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


# ---------------------------------------------------------------- 启动服务
proc = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(PORT)],
    cwd=os.path.dirname(os.path.abspath(__file__)),
    env={**os.environ, "DB_PATH": os.environ["DB_PATH"]},
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
client = httpx.Client(base_url=BASE, timeout=30)
try:
    for _ in range(100):
        try:
            if client.get("/api/health").status_code == 200:
                break
        except httpx.TransportError:
            time.sleep(0.2)
    else:
        print("服务启动失败")
        sys.exit(1)

    def login(username):
        r = client.post("/api/login", json={"username": username})
        assert r.status_code == 200, r.text
        return {"X-Token": r.json()["token"]}

    admin = login("admin")
    zhangwei = login("zhangwei")   # 业务线负责人 · 支付结算
    liuyang = login("liuyang")     # 业务线负责人 · 供应链
    wangqiang = login("wangqiang") # 应用负责人 · 用户增长
    qianyi = login("qianyi")       # 只读观察者 · 用户增长·生产
    sunlei = login("sunlei")       # 应用负责人 · 数据平台（有 data/* 授权）

    me = client.get("/api/me", headers=admin).json()
    assert me["role"] == "admin"

    # 业务线 id
    bls = {b["name"]: b["id"] for b in client.get("/api/business-lines", headers=admin).json()}
    pay_bl = bls["支付结算"]
    users = {u["username"]: u for u in client.get("/api/users", headers=admin).json()}

    def app_id_by_name(name):
        for a in client.get("/api/apps", headers=admin).json():
            if a["name"] == name:
                return a["id"]
        return None

    print("== 问题 1：新建应用重复提交 ==")
    payload = {
        "name": "重复提交测试应用", "business_line_id": pay_bl,
        "owner_id": users["lina"]["id"], "cluster": "华东1集群",
        "environment": "dev", "description": "幂等验证",
    }
    key = {"Idempotency-Key": "verify-key-0001"}
    r1 = client.post("/api/apps", json=payload, headers={**zhangwei, **key})
    check("首次创建 201", r1.status_code == 201, f"{r1.status_code} {r1.text[:200]}")
    r2 = client.post("/api/apps", json=payload, headers={**zhangwei, **key})
    check("同键重试仍成功且是同一个应用", r2.status_code == 201 and r2.json()["id"] == r1.json()["id"],
          f"{r2.status_code} {r2.text[:200]}")
    check("同键重试标记 deduplicated", r2.json().get("deduplicated") is True, r2.text[:200])
    same_name = [a for a in client.get("/api/apps", headers=admin).json() if a["name"] == payload["name"]]
    check("台账里只有一条（没有并排双卡）", len(same_name) == 1, f"实际 {len(same_name)} 条")
    r3 = client.post("/api/apps", json={**payload, "description": "改过的内容"},
                     headers={**zhangwei, **key})
    check("同键但内容变了 → 409 提示先刷新", r3.status_code == 409, f"{r3.status_code}")

    p2 = {**payload, "name": "无键查重测试应用"}
    r4 = client.post("/api/apps", json=p2, headers=zhangwei)
    r5 = client.post("/api/apps", json=p2, headers=zhangwei)
    check("无键同名第二次 → 409（不是 500、不是再建一条）",
          r4.status_code == 201 and r5.status_code == 409, f"{r4.status_code}/{r5.status_code}")

    def post_app(p, extra_headers=None):
        h = dict(zhangwei)
        if extra_headers:
            h.update(extra_headers)
        try:
            r = client.post("/api/apps", json=p, headers=h)
            return r.status_code, r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        except Exception as e:  # noqa: BLE001
            return -1, {"error": str(e)}

    p3 = {**payload, "name": "并发冲刺测试应用"}
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: post_app(p3), range(8)))
    codes = [c for c, _ in results]
    n201, n409 = codes.count(201), codes.count(409)
    same = [a for a in client.get("/api/apps", headers=admin).json() if a["name"] == p3["name"]]
    check("8 路并发同名：恰好 1 个 201、其余 409", n201 == 1 and n409 == 7, str(codes))
    check("8 路并发同名：台账只有一条", len(same) == 1, f"实际 {len(same)} 条")

    p4 = {**payload, "name": "并发同键测试应用"}
    same_key = {"Idempotency-Key": "verify-key-race-01"}
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: post_app(p4, same_key), range(8)))
    codes = [c for c, _ in results]
    ids = {b.get("id") for _, b in results if b.get("id")}
    same = [a for a in client.get("/api/apps", headers=admin).json() if a["name"] == p4["name"]]
    check("8 路并发同幂等键：全部成功且同一个应用", all(c == 201 for c in codes) and len(ids) == 1,
          f"{codes} ids={ids}")
    check("8 路并发同幂等键：台账只有一条", len(same) == 1, f"实际 {len(same)} 条")

    print("== 问题 2：作战台缓存即时失效 ==")
    def console():
        return client.get("/api/console/summary", headers=admin).json()

    def bl_stat(data, name):
        return next(b for b in data["by_business_line"] if b["name"] == name)

    before = console()
    pay0 = bl_stat(before, "支付结算")
    apps0 = before["totals"]["apps"]
    qs_id = app_id_by_name("清结算中心")  # 维保 → 下线
    r = client.post(f"/api/apps/{qs_id}/status", json={"status": "offline"}, headers=zhangwei)
    check("清结算中心 维保→下线 成功", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    after = console()
    pay1 = bl_stat(after, "支付结算")
    check("作战台维保数立即 -1", pay1["maintenance"] == pay0["maintenance"] - 1,
          f"{pay0['maintenance']} → {pay1['maintenance']}")
    check("作战台下线数立即 +1", pay1["offline"] == pay0["offline"] + 1,
          f"{pay0['offline']} → {pay1['offline']}")
    p5 = {**payload, "name": "缓存即时性测试应用"}
    r = client.post("/api/apps", json=p5, headers=zhangwei)
    check("新建应用成功", r.status_code == 201, f"{r.status_code}")
    check("作战台应用总数立即 +1", console()["totals"]["apps"] == apps0 + 1,
          f"{apps0} → {console()['totals']['apps']}")

    print("== 问题 3：负责人空缺 ≠ 放开归属 ==")
    dz_id = app_id_by_name("对账平台")      # 支付结算 · 负责人空缺
    sj_id = app_id_by_name("数据质量中心")   # 数据平台 · 负责人空缺
    check("种子里的缺负责人应用仍在（未动存量）", dz_id is not None and sj_id is not None)

    # 越权尝试前的台账与留痕快照（之后比对一条不动）
    dz_before = client.get(f"/api/apps/{dz_id}", headers=admin).json()
    logs_before = len(dz_before["change_logs"])

    outsider_writes = [
        ("GET 详情", client.get(f"/api/apps/{dz_id}", headers=liuyang)),
        ("PATCH 改信息", client.patch(f"/api/apps/{dz_id}", json={"description": "越权改写"}, headers=liuyang)),
        ("PUT 环境变量", client.put(f"/api/apps/{dz_id}/env-vars", json={"vars": [{"key": "HACK", "value": "1"}]}, headers=liuyang)),
        ("POST 状态流转", client.post(f"/api/apps/{dz_id}/status", json={"status": "online"}, headers=liuyang)),
        ("GET 模块", client.get(f"/api/apps/{dz_id}/modules", headers=liuyang)),
        ("GET 环境", client.get(f"/api/apps/{dz_id}/environments", headers=liuyang)),
        ("POST 建模块", client.post(f"/api/apps/{dz_id}/modules", json={"name": "越权模块"}, headers=liuyang)),
    ]
    for label, r in outsider_writes:
        check(f"别线负责人（刘洋·供应链）{label} → 403", r.status_code == 403,
              f"{r.status_code} {r.text[:160]}")
    check("403 带具体越权原因", "越权" in (outsider_writes[0][1].json().get("detail") or "")
          or "禁止" in (outsider_writes[0][1].json().get("detail") or ""),
          outsider_writes[0][1].text[:160])

    r = client.get(f"/api/apps/{dz_id}", headers=wangqiang)
    check("别线应用负责人（王强·用户增长）看详情 → 403", r.status_code == 403, f"{r.status_code}")
    r = client.get(f"/api/apps/{dz_id}", headers=qianyi)
    check("只读观察者（钱一）看详情 → 403", r.status_code == 403, f"{r.status_code}")
    r = client.get(f"/api/apps/{sj_id}", headers=zhangwei)
    check("支付线负责人看数据平台的缺负责人应用 → 403", r.status_code == 403, f"{r.status_code}")

    # 正常能力不受影响
    r = client.get(f"/api/apps/{dz_id}", headers=zhangwei)
    check("本业务线负责人（张伟·支付结算）看详情 → 200", r.status_code == 200, f"{r.status_code}")
    r = client.patch(f"/api/apps/{dz_id}", json={"description": "本线负责人正常维护"}, headers=zhangwei)
    check("本业务线负责人改信息 → 200", r.status_code == 200, f"{r.status_code} {r.text[:160]}")
    r = client.get(f"/api/apps/{dz_id}", headers=admin)
    check("管理员看详情 → 200", r.status_code == 200, f"{r.status_code}")
    r = client.get(f"/api/apps/{sj_id}", headers=sunlei)
    check("被授权账号（孙磊·数据平台/*）看本线缺负责人应用 → 200", r.status_code == 200, f"{r.status_code}")
    r = client.get(f"/api/apps/{sj_id}/config?environment=dev", headers=sunlei)
    check("被授权账号看该应用配置档案 → 200", r.status_code == 200, f"{r.status_code} {r.text[:160]}")
    r = client.patch(f"/api/apps/{sj_id}", json={"description": "不该成功"}, headers=sunlei)
    check("被授权但非负责人/非线负责人改台账 → 403", r.status_code == 403, f"{r.status_code}")

    # 存量数据与留痕：越权尝试前后完全一致（一条没动）
    dz_after = client.get(f"/api/apps/{dz_id}", headers=admin).json()
    logs_after = len(dz_after["change_logs"])
    check("越权尝试后应用状态未被改动", dz_after["status"] == "developing", dz_after["status"])
    check("越权尝试未写入环境变量", dz_after["env_var_count"] == 0, str(dz_after["env_var_count"]))
    check("越权尝试未产生新留痕", logs_after == logs_before + 1,  # +1 是张伟那次正常维护
          f"{logs_before} → {logs_after}")
    names = [a["name"] for a in client.get("/api/apps", headers=admin).json()]
    check("缺负责人的种子应用一个没少",
          all(n in names for n in ["对账平台", "代付通道服务", "供应商门户", "数据质量中心"]))

    print()
    if failures:
        print(f"失败 {len(failures)} 项：")
        for f in failures:
            print(" -", f)
        sys.exit(1)
    print("全部断言通过 ✅")
finally:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
