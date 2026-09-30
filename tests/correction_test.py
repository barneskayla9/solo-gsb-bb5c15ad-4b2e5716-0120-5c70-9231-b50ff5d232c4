"""误选改判功能端到端 + 并发测试（标准库）。前置：服务已运行在 http://127.0.0.1:8181。

覆盖：
- 正常改判：原申请变未获选、目标变已获选、旧码失效、新码签发；
- 双方查询凭证只显示本人最新状态，新码仅新获选人 / 工作人员可见；
- 理由为空、目标非同物品、目标非未获选、物品已撤下 / 交还、原申请已非获选均拒绝；
- 改判记录留痕；两次改判并发、改判与核码并发只允许一次状态变更生效。
"""
import http.cookiejar
import os
import re
import sqlite3
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = "http://127.0.0.1:8181"
PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def client():
    cj = http.cookiejar.CookieJar()
    return cj, urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))


def _jar_of(op):
    for h in op.handlers:
        if isinstance(h, urllib.request.HTTPCookieProcessor):
            return h.cookiejar
    return http.cookiejar.CookieJar()


def req(op, method, path, form=None, raw=False):
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    try:
        if raw:
            opener = urllib.request.build_opener(
                NoRedirect, urllib.request.HTTPCookieProcessor(_jar_of(op))
            )
            resp = opener.open(r)
            return resp.status, resp.read().decode(), resp.headers.get("Location", "")
        resp = op.open(r)
        return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        if raw:
            return e.code, body, e.headers.get("Location", "")
        return e.code, body


def raw_with_cookie(cookie_header, path, form):
    data = urllib.parse.urlencode(form).encode()
    r = urllib.request.Request(BASE + path, data=data, method="POST")
    r.add_header("Cookie", cookie_header)
    try:
        resp = urllib.request.build_opener(NoRedirect).open(r)
        return resp.status, resp.headers.get("Location", "")
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Location", "")


def csrf(htmltext):
    m = re.search(r'name="csrf_token" value="([^"]+)"', htmltext)
    return m.group(1) if m else None


def submit_claim(answer, contact):
    _, v = client()
    st, body = req(v, "POST", "/items/%d/claims" % ITEM, form={"answer": answer, "contact": contact})
    assert st == 200, body
    m = re.search(r'<div class="tokenbox">([^<]+)</div>', body)
    return m.group(1).strip()


def query_code(token):
    _, v = client()
    st, body = req(v, "GET", f"/query?token={urllib.parse.quote(token)}")
    m = re.search(r'<div class="codebox">(\d{8})</div>', body)
    return st, body, (m.group(1) if m else None)


db = sqlite3.connect("/tmp/lfdata/lostfound.db")

# --------------------------------------------------------------- 登录与造数据
print("== 工作人员登录 ==")
anonjar, anon = client()
st, body = req(anon, "GET", "/staff/login")
tok = csrf(body)
st, _, loc = req(anon, "POST", "/staff/login",
                 form={"csrf_token": tok, "password": "secret123"}, raw=True)
check("工作人员登录(303)", st == 303 and loc == "/staff")
st, dash = req(anon, "GET", "/staff")
SCSRF = csrf(dash)
COOKIE = "; ".join(f"{c.name}={c.value}" for c in anonjar)

PUB, PRIV = "改判测试-黑色钱包", "改判私密-夹层内有门禁卡317"
st, _, loc = req(anon, "POST", "/staff/items",
                 form={"csrf_token": SCSRF, "public_description": PUB,
                       "private_features": PRIV}, raw=True)
check("登记物品1(303)", st == 303 and loc == "/staff/items/1")
ITEM = 1

tA = submit_claim("答案A-说有门禁卡", "13900000001")
tB = submit_claim("答案B-描述不符", "13900000002")
tC = submit_claim("答案C-也对但后到", "13900000003")
st, body = req(anon, "GET", "/staff/items/1")
ids = [int(x) for x in re.findall(r'/staff/claims/(\d+)', body)]
cA, cB, cC = ids[:3]
check("物品1有3份申请", (cA, cB, cC) == (1, 2, 3), str(ids))

# 访客不能调用改判
_, stranger = client()
st, _ = req(stranger, "POST", f"/staff/claims/{cA}/correct",
            form={"to_claim_id": cB, "reason": "x"})
check("未登录改判被拒(403)", st == 403)

# 选定 A
st, _, _ = req(anon, "POST", f"/staff/claims/{cA}/select",
               form={"csrf_token": SCSRF}, raw=True)
check("选定申请A(303)", st == 303)
_, _, code1 = query_code(tA)
check("A 获选并取得交还码", bool(code1), code1)

# 改判表单出现在原获选申请页，目标只列同物品未获选申请
st, page = req(anon, "GET", f"/staff/claims/{cA}")
check("获选申请页有改判表单", f'action="/staff/claims/{cA}/correct"' in page and 'name="reason"' in page)
check("改判目标包含B、C", f'value="{cB}"' in page and f'value="{cC}"' in page)
st, pageB = req(anon, "GET", f"/staff/claims/{cB}")
check("未获选申请页无改判表单", "/correct" not in pageB)

# --------------------------------------------------------------- 拒绝条件
print("== 改判前置校验：不改任何状态 ==")
st, _, loc = req(anon, "POST", f"/staff/claims/{cA}/correct",
                 form={"csrf_token": "bad", "to_claim_id": cB, "reason": "x"}, raw=True)
check("伪造CSRF改判被拒(400)", st == 400)

st, body = req(anon, "POST", f"/staff/claims/{cA}/correct",
               form={"csrf_token": SCSRF, "to_claim_id": cB, "reason": "   "})
check("理由为空被拒且提示", "更正理由不能为空" in body)
check("空理由不产生改判记录", db.execute("SELECT COUNT(*) FROM corrections").fetchone()[0] == 0)
check("空理由后A仍获选",
      db.execute("SELECT status FROM claims WHERE id=?", (cA,)).fetchone()[0] == "selected")

# 另一物品的申请（pending 状态）
req(anon, "POST", "/staff/items",
    form={"csrf_token": SCSRF, "public_description": "另一物品", "private_features": "x"}, raw=True)
_, v2 = client()
st, b2 = req(v2, "POST", "/items/2/claims", form={"answer": "外人", "contact": "z"})
other = int(re.search(r'/staff/claims/(\d+)',
                      req(anon, "GET", "/staff/items/2")[1]).group(1))
st, body = req(anon, "POST", f"/staff/claims/{cA}/correct",
               form={"csrf_token": SCSRF, "to_claim_id": other, "reason": "改给别人"})
check("目标不属同一物品被拒", "不属于同一物品" in body)

# 目标必须为未获选（人为插入一份同物品 pending 申请）
db.execute(
    "INSERT INTO claims(item_id,answer,contact,token_hash,status,created_at,decided_at)"
    " VALUES(1,'迟到的pending','p','pending-hash-x','pending','2026-01-01T00:00:00+00:00',NULL)")
db.commit()
cP = db.execute("SELECT id FROM claims WHERE token_hash='pending-hash-x'").fetchone()[0]
st, body = req(anon, "POST", f"/staff/claims/{cA}/correct",
               form={"csrf_token": SCSRF, "to_claim_id": cP, "reason": "改给待核对"})
check("目标为待核对(pending)被拒", "目标申请必须为未获选" in body)

# --------------------------------------------------------------- 正常改判
print("== 正常改判 A -> B ==")
REASON = "经复核A的答案与私密门禁卡特征不符，B描述一致"
st, _, loc = req(anon, "POST", f"/staff/claims/{cA}/correct",
                 form={"csrf_token": SCSRF, "to_claim_id": cB, "reason": REASON}, raw=True)
check("改判成功(303跳到目标申请)", st == 303 and loc == f"/staff/claims/{cB}", loc)

row = db.execute("SELECT status FROM claims WHERE id=?", (cA,)).fetchone()[0]
check("原申请A变为未获选", row == "rejected")
row = db.execute("SELECT status FROM claims WHERE id=?", (cB,)).fetchone()[0]
check("目标申请B变为已获选", row == "selected")
nsel = db.execute("SELECT COUNT(*) FROM claims WHERE item_id=1 AND status='selected'").fetchone()[0]
check("物品1仍恰好1份获选", nsel == 1)
old_inv = db.execute(
    "SELECT invalidated_at FROM handover_codes WHERE claim_id=? ORDER BY id LIMIT 1", (cA,)
).fetchone()[0]
check("A的旧码立即标记失效", bool(old_inv))
new = db.execute(
    "SELECT id FROM handover_codes WHERE claim_id=?", (cB,)
).fetchall()
check("为B签发1枚新码", len(new) == 1)

st, qa, ca = query_code(tA)
check("A凭证看到未获选", "未获选" in qa and "已选定其他获选人" in qa)
check("A查询页看不到任何交还码", ca is None and 'class="codebox"' not in qa)
st, qb, code2 = query_code(tB)
check("B凭证看到已获选和新码", "已获选" in qb and bool(code2))
check("新码与旧码不同", code2 != code1)
st, qc, cc = query_code(tC)
check("C仍未获选且无码", "未获选" in qc and cc is None)

st, sp = req(anon, "GET", f"/staff/claims/{cB}")
check("工作人员在B详情页可见新码", f'<div class="codebox">{code2}</div>' in sp)
st, sa = req(anon, "GET", f"/staff/claims/{cA}")
check("工作人员在A详情页看不到新码明文", code2 not in sa)

# 旧码核销被拒
st, vf = req(anon, "GET", "/staff/verify")
st, body = req(anon, "POST", "/staff/verify",
               form={"csrf_token": csrf(vf), "code": code1})
check("旧码（改判后）核销被拒", "已失效" in body)

# 留痕
cr = db.execute("SELECT from_claim_id,to_claim_id,reason,created_at FROM corrections").fetchall()
check("改判记录恰好1条且字段完整",
      len(cr) == 1 and tuple(cr[0][:3]) == (cA, cB, REASON) and bool(cr[0][3]))
st, body = req(anon, "GET", "/staff/items/1")
check("物品详情页有改判记录(双方+理由)",
      "改判记录" in body and f"#{cA}" in body and f"#{cB}" in body and REASON in body)
st, body = req(anon, "GET", f"/staff/claims/{cA}")
check("A申请页有改判记录", "改判记录" in body and REASON in body)

# 原申请已非获选再改判
st, body = req(anon, "POST", f"/staff/claims/{cA}/correct",
               form={"csrf_token": SCSRF, "to_claim_id": cC, "reason": "再改一次"})
check("对已落选的A再改判被拒", "已非获选" in body or "不是已获选" in body)

# 新码核销成功，物品交还；交还后改判必须被拒
st, vf = req(anon, "GET", "/staff/verify")
st, _, loc = req(anon, "POST", "/staff/verify",
                 form={"csrf_token": csrf(vf), "code": code2}, raw=True)
check("新码核销成功(303)", st == 303 and loc == "/staff/items/1")
st, body = req(anon, "POST", f"/staff/claims/{cB}/correct",
               form={"csrf_token": SCSRF, "to_claim_id": cC, "reason": "交还后改判"})
check("物品已交还拒绝改判", "已撤下或已交还" in body)
check("交还后状态不变(B仍获选)",
      db.execute("SELECT status FROM claims WHERE id=?", (cB,)).fetchone()[0] == "selected")

# --------------------------------------------------------------- 撤下后改判
print("== 已撤下物品拒绝改判 ==")
req(anon, "POST", "/staff/items",
    form={"csrf_token": SCSRF, "public_description": "撤下测试物品",
          "private_features": "p"}, raw=True)
_, v3 = client()
req(v3, "POST", "/items/3/claims", form={"answer": "gA", "contact": "1"})
_, v3 = client()
req(v3, "POST", "/items/3/claims", form={"answer": "gB", "contact": "2"})
ids3 = [int(x) for x in re.findall(r'/staff/claims/(\d+)', req(anon, "GET", "/staff/items/3")[1])]
gA, gB = ids3[0], ids3[1]
req(anon, "POST", f"/staff/claims/{gA}/select", form={"csrf_token": SCSRF}, raw=True)
req(anon, "POST", "/staff/items/3/remove", form={"csrf_token": SCSRF}, raw=True)
st, body = req(anon, "POST", f"/staff/claims/{gA}/correct",
               form={"csrf_token": SCSRF, "to_claim_id": gB, "reason": "撤下后改判"})
check("物品已撤下拒绝改判", "已撤下或已交还" in body)
check("撤下后无新增改判记录", db.execute("SELECT COUNT(*) FROM corrections").fetchone()[0] == 1)

# --------------------------------------------------------------- 并发：两次改判
print("== 两次改判并发：至多一次生效 ==")
req(anon, "POST", "/staff/items",
    form={"csrf_token": SCSRF, "public_description": "并发改判物品",
          "private_features": "p"}, raw=True)
IID = 4
for ans in ("eA", "eB", "eC"):
    _, vv = client()
    req(vv, "POST", f"/items/{IID}/claims", form={"answer": ans, "contact": ans})
ids4 = [int(x) for x in re.findall(r'/staff/claims/(\d+)', req(anon, "GET", f"/staff/items/{IID}")[1])]
eA, eB, eC = ids4
req(anon, "POST", f"/staff/claims/{eA}/select", form={"csrf_token": SCSRF}, raw=True)
barrier = threading.Barrier(2)


def correct_task(target):
    barrier.wait()
    return raw_with_cookie(COOKIE, f"/staff/claims/{eA}/correct",
                           {"csrf_token": SCSRF, "to_claim_id": target, "reason": f"改给{target}"})


with ThreadPoolExecutor(max_workers=2) as ex:
    r1, r2 = [f.result() for f in [ex.submit(correct_task, eB), ex.submit(correct_task, eC)]]
winners = [loc for code, loc in (r1, r2) if code == 303 and loc in (f"/staff/claims/{eB}", f"/staff/claims/{eC}")]
check("并发改判恰好1次落到目标页", len(winners) == 1, f"{r1} {r2}")
nsel = db.execute("SELECT COUNT(*) FROM claims WHERE item_id=? AND status='selected'", (IID,)).fetchone()[0]
nrej = db.execute("SELECT COUNT(*) FROM claims WHERE item_id=? AND status='rejected'", (IID,)).fetchone()[0]
ncorr = db.execute("SELECT COUNT(*) FROM corrections WHERE item_id=?", (IID,)).fetchone()[0]
nactive = db.execute(
    "SELECT COUNT(*) FROM handover_codes hc JOIN claims c ON c.id=hc.claim_id"
    " WHERE c.item_id=? AND hc.invalidated_at IS NULL AND hc.consumed_at IS NULL", (IID,)).fetchone()[0]
check("并发后仅1份获选、2份落选", nsel == 1 and nrej == 2, f"{nsel}/{nrej}")
check("并发后仅1条改判记录", ncorr == 1)
check("并发后全物品仅1枚有效新码", nactive == 1)
check("eA必为落选", db.execute("SELECT status FROM claims WHERE id=?", (eA,)).fetchone()[0] == "rejected")

# --------------------------------------------------------------- 并发：改判 vs 核码
print("== 改判与核码并发：只允许一次状态变更 ==")
req(anon, "POST", "/staff/items",
    form={"csrf_token": SCSRF, "public_description": "改判核码竞争物品",
          "private_features": "p"}, raw=True)
JID = 5
_, vv = client()
req(vv, "POST", f"/items/{JID}/claims", form={"answer": "fA", "contact": "fA"})
_, vv = client()
req(vv, "POST", f"/items/{JID}/claims", form={"answer": "fB", "contact": "fB"})
ids5 = [int(x) for x in re.findall(r'/staff/claims/(\d+)', req(anon, "GET", f"/staff/items/{JID}")[1])]
fA, fB = ids5
req(anon, "POST", f"/staff/claims/{fA}/select", form={"csrf_token": SCSRF}, raw=True)
# 直接取 fA 当前码明文
from lostfound.security import decrypt_field, load_or_create_secret
secret = load_or_create_secret("/tmp/lfdata/secret.key")
enc = db.execute("SELECT code_enc FROM handover_codes WHERE claim_id=? ORDER BY id DESC LIMIT 1",
                 (fA,)).fetchone()[0]
fcode = decrypt_field(secret, enc)
barrier2 = threading.Barrier(2)


def do_correct():
    barrier2.wait()
    return raw_with_cookie(COOKIE, f"/staff/claims/{fA}/correct",
                           {"csrf_token": SCSRF, "to_claim_id": fB, "reason": "与核码竞争"})


def do_verify():
    barrier2.wait()
    return raw_with_cookie(COOKIE, "/staff/verify", {"csrf_token": SCSRF, "code": fcode})


with ThreadPoolExecutor(max_workers=2) as ex:
    cr1, vr1 = [f.result() for f in [ex.submit(do_correct), ex.submit(do_verify)]]

istat = db.execute("SELECT status FROM items WHERE id=?", (JID,)).fetchone()[0]
ncorr = db.execute("SELECT COUNT(*) FROM corrections WHERE item_id=?", (JID,)).fetchone()[0]
nconsumed = db.execute(
    "SELECT COUNT(*) FROM handover_codes hc JOIN claims c ON c.id=hc.claim_id"
    " WHERE c.item_id=? AND hc.consumed_at IS NOT NULL", (JID,)).fetchone()[0]
sel = db.execute("SELECT id FROM claims WHERE item_id=? AND status='selected'", (JID,)).fetchall()
consistent = (
    (istat == "returned" and ncorr == 0 and nconsumed == 1 and [r[0] for r in sel] == [fA])
    or (istat == "kept" and ncorr == 1 and nconsumed == 0 and [r[0] for r in sel] == [fB])
)
check("改判与核码结果互斥且状态一致", consistent,
      f"item={istat} corr={ncorr} consumed={nconsumed} selected={[r[0] for r in sel]} {cr1} {vr1}")

print(f"\n结果：{PASS} 通过, {FAIL} 失败")
raise SystemExit(1 if FAIL else 0)
