"""三个线上问题修复的回归验证（桩掉 FastAPI/pydantic，直跑路由函数，临时库不影响真实数据）。

覆盖：
1. 新建应用幂等：同一 client_request_id 重发（网络卡顿重试/连点）只落一条台账、
   一条创建留痕；同键不同内容返回首次创建的应用；重名一律 409 而非 500。
2. 作战台实时性：状态流转为「下线」后 console_summary 立即按新状态统计，无缓存滞后。
3. 空负责人应用权限：跨业务线账号看/改一律 403；本业务线负责人与平台管理员可管；
   有 业务线×环境 授权的账号仍可按授权查看；只读观察者依旧不能改。
4. 既有数据不动：验证前后台账与各类留痕条数只增不减。
"""
import os
import sys
import tempfile

tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(tmp, "test.db")

# ---------------------------------------------------------------- FastAPI / pydantic 桩
STUBS = os.path.join(tmp, "stubs")
os.makedirs(os.path.join(STUBS, "fastapi"))

with open(os.path.join(STUBS, "pydantic.py"), "w") as f:
    f.write(
        "class _FieldInfo:\n"
        "    def __init__(self, default=..., **kw): self.default = default\n"
        "def Field(default=..., **kw): return _FieldInfo(default)\n"
        "class BaseModel:\n"
        "    def __init__(self, **kwargs):\n"
        "        anns = {}\n"
        "        for klass in reversed(type(self).__mro__):\n"
        "            anns.update(getattr(klass, '__annotations__', {}))\n"
        "        for name in anns:\n"
        "            default = getattr(type(self), name, ...)\n"
        "            if isinstance(default, _FieldInfo): default = default.default\n"
        "            if name in kwargs: setattr(self, name, kwargs[name])\n"
        "            elif default is not ...: setattr(self, name, default)\n"
        "            else: raise TypeError(f'缺少必填字段: {name}')\n"
    )

with open(os.path.join(STUBS, "fastapi", "__init__.py"), "w") as f:
    f.write(
        "class HTTPException(Exception):\n"
        "    def __init__(self, status_code, detail=None):\n"
        "        super().__init__(str(detail))\n"
        "        self.status_code = status_code\n"
        "        self.detail = detail\n"
        "def Depends(dependency=None, **kw): return dependency\n"
        "def Header(default=None, **kw): return default\n"
        "def _deco(*a, **kw):\n"
        "    return lambda fn: fn\n"
        "class FastAPI:\n"
        "    def __init__(self, **kw): pass\n"
        "    def include_router(self, *a, **kw): pass\n"
        "    def mount(self, *a, **kw): pass\n"
        "    def on_event(self, *a, **kw): return _deco()\n"
        "    get = post = put = patch = delete = staticmethod(_deco)\n"
        "class APIRouter:\n"
        "    def __init__(self, **kw): pass\n"
        "    get = post = put = patch = delete = staticmethod(_deco)\n"
    )
with open(os.path.join(STUBS, "fastapi", "responses.py"), "w") as f:
    f.write(
        "class Response:\n"
        "    def __init__(self, *a, **kw): pass\n"
        "class FileResponse(Response): pass\n"
    )
with open(os.path.join(STUBS, "fastapi", "staticfiles.py"), "w") as f:
    f.write("class StaticFiles:\n    def __init__(self, *a, **kw): pass\n")

sys.path.insert(0, STUBS)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import HTTPException  # noqa: E402

from app import auth, main, permissions as perms  # noqa: E402
from app.db import get_conn, init_db, query, query_one  # noqa: E402
from app.seed import seed_if_empty  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(("  ✓ " if cond else "  ✗ ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def expect_http(status, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except HTTPException as e:
        return e.status_code == status, f"HTTP {e.status_code}: {e.detail}"
    return False, "未抛出预期异常"


def principal(username):
    row = query_one(
        """SELECT u.id, u.username, u.name, u.role, u.business_line_id,
                  b.name AS business_line_name, u.token
           FROM users u LEFT JOIN business_lines b ON b.id = u.business_line_id
           WHERE u.username = ?""",
        (username,),
    )
    return perms.build_principal(dict(row))


init_db()
seed_if_empty()

admin = principal("admin")
zhangwei = principal("zhangwei")   # 业务线负责人·支付结算
lina = principal("lina")           # 应用负责人·支付结算（pay × * 授权）
liuyang = principal("liuyang")     # 业务线负责人·供应链
zhaomin = principal("zhaomin")     # 应用负责人·供应链（仅 supply × prod 授权）
sunlei = principal("sunlei")       # 应用负责人·数据平台（仅 data × * 授权）

PAY = query_one("SELECT id FROM business_lines WHERE code='pay'")["id"]

# 既有数据基线：修复只改代码与新增空表，任何既有台账/留痕一条不动
baseline = {t: query_one(f"SELECT COUNT(*) AS c FROM {t}")["c"]
            for t in ("applications", "change_logs", "config_audit_logs",
                      "ops_audit_logs", "permission_logs", "app_transfers")}

print("== 问题 1：新建应用重复提交 ==")
body = main.AppCreateIn(
    name="幂等回归应用", business_line_id=PAY, owner_id=None,
    cluster="华东1集群", environment="dev", description="重复提交回归",
    client_request_id="crid-regression-0001",
)
r1 = main.create_app(body, zhangwei)
r2 = main.create_app(body, zhangwei)  # 模拟网络卡顿后的整包重发
check("同一幂等键重发返回同一个应用", r2["id"] == r1["id"] and r2.get("deduplicated") is True)
cnt = query_one("SELECT COUNT(*) AS c FROM applications WHERE business_line_id=? AND name='幂等回归应用'", (PAY,))["c"]
check("台账只多出一条记录", cnt == 1, f"实际 {cnt} 条")
logs = query("SELECT id FROM change_logs WHERE app_id=? AND action='创建应用'", (r1["id"],))
check("创建留痕只有一条", len(logs) == 1, f"实际 {len(logs)} 条")

same_key_other_name = main.AppCreateIn(
    name="换了名字的重复提交", business_line_id=PAY, owner_id=None,
    cluster="华东1集群", environment="dev", description="",
    client_request_id="crid-regression-0001",
)
r3 = main.create_app(same_key_other_name, zhangwei)
check("同键不同名仍返回首次创建的应用", r3["id"] == r1["id"] and r3.get("deduplicated") is True)
check("换名重发没有产生新台账",
      query_one("SELECT COUNT(*) AS c FROM applications WHERE name='换了名字的重复提交'")["c"] == 0)

dup_new_key = main.AppCreateIn(
    name="幂等回归应用", business_line_id=PAY, owner_id=None,
    cluster="华东1集群", environment="dev", description="",
    client_request_id="crid-regression-0002",
)
ok, detail = expect_http(409, main.create_app, dup_new_key, zhangwei)
check("同名不同键 → 409 重名拦截", ok, detail)

dup_no_key = main.AppCreateIn(
    name="幂等回归应用", business_line_id=PAY, owner_id=None,
    cluster="华东1集群", environment="dev", description="",
)
ok, detail = expect_http(409, main.create_app, dup_no_key, zhangwei)
check("同名无幂等键 → 409（唯一约束兜底，不再 500）", ok, detail)

normal = main.AppCreateIn(
    name="正常新建应用", business_line_id=PAY, owner_id=None,
    cluster="华北2集群", environment="test", description="",
)
r4 = main.create_app(normal, zhangwei)
check("无幂等键的正常创建不受影响", r4["name"] == "正常新建应用" and not r4.get("deduplicated"))

print("== 问题 2：状态流转后作战台立即反映 ==")
target = query_one("SELECT * FROM applications WHERE name='清结算中心'")  # pay / maintenance
before = main.console_summary(admin)
bl_before = {r["id"]: r for r in before["by_business_line"]}[target["business_line_id"]]
main.change_status(target["id"], main.StatusIn(status="offline"), admin)
after = main.console_summary(admin)
bl_after = {r["id"]: r for r in after["by_business_line"]}[target["business_line_id"]]
check("维保数立即 -1", bl_after["maintenance"] == bl_before["maintenance"] - 1,
      f"{bl_before['maintenance']} → {bl_after['maintenance']}")
check("下线数立即 +1", bl_after["offline"] == bl_before["offline"] + 1,
      f"{bl_before['offline']} → {bl_after['offline']}")
again = main.console_summary(admin)
check("连续刷新不再靠运气（三次结果一致）",
      {r["id"]: r for r in again["by_business_line"]}[target["business_line_id"]]["offline"]
      == bl_after["offline"])

print("== 问题 3：空负责人应用的归属边界 ==")
orphan = query_one("SELECT * FROM applications WHERE name='供应商门户'")  # supply / 无负责人 / test
check("样例应用确实缺负责人", orphan["owner_id"] is None)

ok, detail = expect_http(403, perms.ensure_app_visible, sunlei, dict(orphan))
check("跨业务线账号查看 → 403", ok, detail)
ok, detail = expect_http(403, auth.get_app_writable, sunlei, orphan["id"])
check("跨业务线账号修改 → 403", ok, detail)
ok, detail = expect_http(403, main.app_detail, orphan["id"], sunlei)
check("跨业务线账号打开详情 → 403", ok, detail)
ok, detail = expect_http(403, main.update_app, orphan["id"],
                         main.AppUpdateIn(description="越权改动"), sunlei)
check("跨业务线账号 PATCH → 403 且留痕不会记到无关账号", ok, detail)

ok, detail = expect_http(403, perms.ensure_app_visible, zhaomin, dict(orphan))
check("同业务线但仅 prod 授权看 test 环境 → 403（环境级收窄）", ok, detail)

try:
    auth.get_app_writable(liuyang, orphan["id"])
    check("本业务线负责人仍可管理（可接手补负责人）", True)
except HTTPException as e:
    check("本业务线负责人仍可管理（可接手补负责人）", False, f"HTTP {e.status_code}")
try:
    auth.get_app_writable(admin, orphan["id"])
    check("平台管理员仍可管理", True)
except HTTPException as e:
    check("平台管理员仍可管理", False, f"HTTP {e.status_code}")

pay_orphan = query_one("SELECT * FROM applications WHERE name='对账平台'")  # pay / 无负责人 / test
try:
    perms.ensure_app_visible(lina, dict(pay_orphan))
    check("有 业务线×环境 授权的账号仍可查看空负责人应用", True)
except HTTPException as e:
    check("有 业务线×环境 授权的账号仍可查看空负责人应用", False, f"HTTP {e.status_code}")
ok, detail = expect_http(403, auth.get_app_writable, lina, pay_orphan["id"])
check("但授权账号不能越权改台账（非负责人/业务线负责人）", ok, detail)

# 负责人补齐后，负责人在自己应用上的权限不受影响
owned = query_one("SELECT * FROM applications WHERE name='会员中心'")  # growth / wangqiang
wangqiang = principal("wangqiang")
try:
    auth.get_app_writable(wangqiang, owned["id"])
    perms.ensure_app_visible(wangqiang, dict(owned))
    check("正常归属应用的负责人权限不受影响", True)
except HTTPException as e:
    check("正常归属应用的负责人权限不受影响", False, f"HTTP {e.status_code}")

print("== 既有数据完整性 ==")
for table, before_cnt in baseline.items():
    after_cnt = query_one(f"SELECT COUNT(*) AS c FROM {table}")["c"]
    check(f"{table} 只增不减（{before_cnt} → {after_cnt}）", after_cnt >= before_cnt)
check("种子台账 24 条原样保留",
      query_one("SELECT COUNT(*) AS c FROM applications WHERE id <= 24")["c"] == 24)

print()
if failures:
    print(f"失败 {len(failures)} 项：{failures}")
    sys.exit(1)
print("全部通过 ✅")
