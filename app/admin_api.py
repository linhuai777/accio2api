"""管理端 API —— 凭证管理 + 两种添加模式 + 状态查询。

对应 codebuddy2api 的管理端契约（保持同类项目的使用习惯一致）：

    GET    /admin/api/credentials                 凭证列表
    POST   /admin/api/credentials                 模式一 B：粘贴 cookie 添加
    DELETE /admin/api/credentials/{key}           删除
    POST   /admin/api/credentials/{key}/enable    启用/停用
    POST   /admin/api/credentials/{key}/sync      重新拉模型 + 账号信息

    POST   /admin/api/login/start                 模式一 A：开网页登录会话
    GET    /admin/api/login/{id}                  轮询状态
    GET    /admin/api/login/{id}/frame            取当前画面（jpeg）
    POST   /admin/api/login/{id}/input            回传鼠标/键盘事件
    POST   /admin/api/login/{id}/finish           登录完成 → 落库
    DELETE /admin/api/login/{id}                  取消

    POST   /admin/api/register                    模式二：全自动注册（CloudMail）
"""

from __future__ import annotations

import hmac
import json
import threading
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from .auth.otp import get_backend
from .auth.web_login import web_login
from .core.config import settings
from .core.credentials import Credential, pool
from .upstream.client import AccioClient

router = APIRouter()


# ── 鉴权 ────────────────────────────────────────────────────
def _admin_guard(request: Request):
    tok = ""
    a = request.headers.get("authorization")
    if a and a.lower().startswith("bearer "):
        tok = a[7:].strip()
    else:
        tok = request.headers.get("x-api-key", "")
    # 🔴 恒定时间比较：`!=` 会在首个不同字节处短路返回，逐字节爆破
    #    理论上可行。用 hmac.compare_digest 抹平时间差。
    if not tok or not hmac.compare_digest(tok, settings.admin_key or ""):
        raise HTTPException(401, detail={"error": {
            "message": "Admin key required",
            "type": "invalid_request_error", "code": "invalid_admin_key"}})


# ── 凭证列表 ────────────────────────────────────────────────
@router.get("/admin/api/credentials")
def list_credentials(request: Request):
    _admin_guard(request)
    items = []
    for c in pool.all():
        d = c.to_public()
        d["expired"] = _looks_expired(c)
        items.append(d)
    return {"data": items, "total": len(items)}


def _looks_expired(c: Credential) -> bool:
    """粗判：最近一次验证是否失败。真正的过期判定靠 sync 实测。"""
    return bool(c.last_error)


# ── 模式一 B：粘贴 cookie ───────────────────────────────────
@router.post("/admin/api/credentials")
async def add_credential(request: Request):
    """直接提交 cookies 添加凭证（形态最轻，适合能拿到 cookie 的场景）。

    body:
      {"cookies": {"_m_h5_tk": "...", "cookie2": "...", ...},
       "label": "可选备注"}
    """
    _admin_guard(request)
    body = await request.json()
    cookies = body.get("cookies") or {}
    if not isinstance(cookies, dict) or len(cookies) < 3:
        raise HTTPException(400, detail={"error": {
            "message": "cookies 必须是非空字典（建议包含 _m_h5_tk / cookie2）",
            "type": "invalid_request_error"}})

    client = AccioClient(cookies=cookies)
    try:
        info = client.fetch_userinfo()
    except Exception as e:
        raise HTTPException(400, detail={"error": {
            "message": f"cookie 校验失败：{str(e)[:200]}",
            "type": "invalid_credential"}})

    uid = str(info.get("userId") or info.get("uid") or "")
    if not uid:
        raise HTTPException(400, detail={"error": {
            "message": "无法从 cookie 解析出用户身份",
            "type": "invalid_credential"}})

    cred = Credential(
        user_id=uid,
        accio_id=str(info.get("accioId") or ""),
        nickname=str(info.get("nickname") or info.get("nickName") or ""),
        email=str(info.get("email") or ""),
        cookies=cookies,
    )
    _enrich(cred, client)
    pool.save(cred)
    return {"data": cred.to_public(), "message": "凭证已添加"}


def _enrich(cred: Credential, client: AccioClient):
    """补齐运行时字段：agent_id / project_path / 模型可用性 / 积分。

    端点依据（实测 2026-09，随站点版本可能变化，故每个都独立 try）：
      GET /gateway/auth/userinfo  → userId / accioId / userName / userLevel / routeRegion
      GET /gateway/agents         → agent 列表（元素 id 即 agentId）
      GET /gateway/workspace      → 工作区（元素 path 即云电脑路径）
      GET /gateway/models         → 模型池（顺带验证 cookie 是否仍有效）
    """
    info = client.fetch_userinfo()
    cred.user_id = str(info.get("userId") or cred.user_id)
    cred.accio_id = str(info.get("accioId") or cred.accio_id)
    # 注意：字段是 userName，不是 nickname
    cred.nickname = str(info.get("userName") or info.get("nickname") or "")
    cred.email = str(info.get("email") or info.get("desensitizedEmail")
                     or cred.email)
    cred.credits = {
        k: info.get(k) for k in
        ("userLevel", "routeRegion", "mobileBound", "needBindMobile",
         "desensitizedEmail")
        if info.get(k) is not None
    }

    # agent 列表 → agentId
    try:
        r = client.http().get(f"{settings.base_url}/gateway/agents?lang=zh",
                              timeout=20)
        if r.status_code == 200:
            agents = r.json().get("data") or []
            if agents:
                cred.agent_id = str(agents[0].get("id")
                                    or agents[0].get("agentId") or "")
    except Exception:
        pass

    # workspace → 云电脑工作区路径
    try:
        r = client.http().get(
            f"{settings.base_url}/gateway/workspace?orderBy=createdAt"
            f"&order=desc&{client._qs()}", timeout=20)
        if r.status_code == 200:
            ws = r.json().get("data") or []
            if ws:
                cred.project_path = str(ws[0].get("path") or "")
    except Exception:
        pass

    # ⚠ 不要在这里"猜" project_path。
    #
    # 曾经这里有一段硬编码兜底：
    #     f"/home/{某用户名}/.accio/accounts/{uid}/agents/{aid}/project"
    # 这是错的，两个理由：
    #   1. **泄露**：那个用户名属于开发者的运行环境，不是本项目的常量。
    #      （它其实是上游云沙箱的容器路径，属于运行时数据 —— 见 config.py 原则 1）
    #   2. **功能错误**：别人的沙箱用户名/目录布局不同，兜底算出的路径必然对不上，
    #      反而会让调用静默失败。
    #
    # 正确来源只有两处，且都是运行时获取：
    #   a) `/gateway/workspace`（上面已取）—— 权威
    #   b) 用户粘贴 cookie 时一并填写的 project_path（core/credentials.py）
    # 两者都没有时，就把空值带下去，让上游自己决定 —— 不猜。

    # 模型池（同时说明 cookie 有效）
    models = client.fetch_models()
    cred.credits["model_count"] = len(models)
    cred.last_verified_at = time.time()
    cred.last_error = ""


# ── 删除 / 启停 ─────────────────────────────────────────────
@router.delete("/admin/api/credentials/{account_key}")
def delete_credential(account_key: str, request: Request):
    _admin_guard(request)
    if not pool.delete(account_key):
        raise HTTPException(404, detail={"error": {"message": "凭证不存在"}})
    return {"message": "已删除"}


@router.post("/admin/api/credentials/{account_key}/enable")
async def enable_credential(account_key: str, request: Request):
    _admin_guard(request)
    body = await request.json()
    if not pool.set_enabled(account_key, bool(body.get("enabled", True))):
        raise HTTPException(404, detail={"error": {"message": "凭证不存在"}})
    return {"message": "已更新"}


@router.post("/admin/api/credentials/{account_key}/sync")
def sync_credential(account_key: str, request: Request):
    """重新拉取账号信息 / 模型 / 余额，用于验证 cookie 是否仍有效。"""
    _admin_guard(request)
    cred = pool.get(account_key)
    if not cred:
        raise HTTPException(404, detail={"error": {"message": "凭证不存在"}})
    client = AccioClient(user_id=cred.user_id, accio_id=cred.accio_id,
                         cookies=dict(cred.cookies),
                         agent_id=cred.agent_id, project_path=cred.project_path,
                         account_key=cred.account_key)
    try:
        _enrich(cred, client)
        cred.last_error = ""
        pool.save(cred)
    except Exception as e:
        cred.last_error = str(e)[:300]
        pool.save(cred)
        raise HTTPException(502, detail={"error": {
            "message": f"同步失败（cookie 可能已过期）：{str(e)[:200]}"}})
    return {"data": cred.to_public(), "message": "同步完成"}


# ── 模式一 A：网页登录 ──────────────────────────────────────
@router.post("/admin/api/login/start")
async def login_start(request: Request):
    """开一个网页登录会话，返回 login_id。

    前端流程：
      1. POST 这里 → 拿 login_id
      2. <img> 指向 /admin/api/login/{id}/frame?ts=<时间戳> 循环刷新（或 WebSocket）
      3. 在图上操作 → 事件 POST 到 /input
      4. 轮询 GET /admin/api/login/{id} 直到 stage == "done"
      5. POST /finish 落库
    """
    _admin_guard(request)
    if not settings.browser_enabled:
        raise HTTPException(503, detail={"error": {
            "message": "服务器未启用浏览器（BROWSER_ENABLED=false）",
            "type": "server_error"}})
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    email = str(body.get("email") or "").strip()
    s = web_login.start(email)
    return {"data": s.snapshot(), "message": "登录会话已创建"}


@router.get("/admin/api/login/{login_id}")
def login_status(login_id: str, request: Request):
    _admin_guard(request)
    s = web_login.get(login_id)
    if not s:
        raise HTTPException(404, detail={"error": {"message": "登录会话不存在"}})
    return {"data": s.snapshot()}


@router.get("/admin/api/login/{login_id}/frame")
def login_frame(login_id: str, request: Request):
    """返回当前画面 JPEG。前端用 <img> 轮询即可（无需 WebSocket）。

    ⚠️ 安全：本端点会把管理员正在操作的浏览器画面（含邮箱、验证码、
       二维码）以图片流返回，**必须**与同组其余端点一致地强制 ADMIN_KEY，
       否则未认证者可凭 login_id 实时窥屏。
    """
    _admin_guard(request)
    if not settings.browser_enabled:
        raise HTTPException(503, detail={"error": {"message": "浏览器能力已关闭"}})
    s = web_login.get(login_id)
    if not s:
        raise HTTPException(404, detail={"error": {"message": "登录会话不存在"}})
    data = s.latest_frame()
    if not data:
        raise HTTPException(503, detail={"error": {"message": "画面尚未就绪"}})
    return Response(content=data, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@router.post("/admin/api/login/{login_id}/input")
async def login_input(login_id: str, request: Request):
    """前端把鼠标/键盘事件回传（见 web_login.forward_input 的格式）。

    ⚠️ 安全：本端点可向管理员的浏览器注入鼠标/键盘事件，进而把登录态
       固化为网关凭证（配合 /finish）。必须强制 ADMIN_KEY。
    """
    _admin_guard(request)
    if not settings.browser_enabled:
        raise HTTPException(503, detail={"error": {"message": "浏览器能力已关闭"}})
    s = web_login.get(login_id)
    if not s:
        raise HTTPException(404, detail={"error": {"message": "登录会话不存在"}})
    try:
        evt = await request.json()
    except Exception:
        raise HTTPException(400, detail={"error": {"message": "invalid event"}})
    ok = web_login.forward_input(login_id, evt)
    return {"ok": ok}


@router.post("/admin/api/login/{login_id}/finish")
def login_finish(login_id: str, request: Request):
    """登录成功后落库：把会话里捕获的 cookie 存成凭证。"""
    _admin_guard(request)
    s = web_login.get(login_id)
    if not s:
        raise HTTPException(404, detail={"error": {"message": "登录会话不存在"}})
    if not s.result_cookies:
        raise HTTPException(400, detail={"error": {
            "message": "尚未检测到登录完成（stage=%s）" % s.stage}})

    client = AccioClient(cookies=dict(s.result_cookies))
    try:
        info = client.fetch_userinfo()
    except Exception as e:
        raise HTTPException(502, detail={"error": {
            "message": f"登录态校验失败：{str(e)[:200]}"}})

    cred = Credential(
        user_id=str(info.get("userId") or ""),
        accio_id=str(info.get("accioId") or ""),
        nickname=str(info.get("nickname") or ""),
        email=str(info.get("email") or s.email),
        cookies=dict(s.result_cookies),
    )
    _enrich(cred, client)
    pool.save(cred)
    s.close()
    return {"data": cred.to_public(), "message": "登录成功，凭证已保存"}


@router.delete("/admin/api/login/{login_id}")
def login_cancel(login_id: str, request: Request):
    _admin_guard(request)
    s = web_login.get(login_id)
    if not s:
        raise HTTPException(404, detail={"error": {"message": "登录会话不存在"}})
    s.close()
    return {"message": "已取消"}


# ── 模式二：全自动注册 ──────────────────────────────────────
@router.post("/admin/api/register")
async def auto_register(request: Request):
    """全自动注册：浏览器过滑块 + 邮箱自动收码 + 落库。

    body:
        {
          "email":   可选。显式指定注册地址（给定则直接用，不派生别名）
          "count":   可选。批量注册个数（默认 1；需已配 MAIL_PRIMARY 才能 >1）
          "label":   可选。标签前缀，用于批量时的可读命名
        }

    别名机制（配了 `MAIL_PRIMARY` 时生效）：
        未显式给 `email` 时，按 `count` 自动从主邮箱派生独立别名，
        每个账号一个地址、全部投递到同一收件箱。这样上游看到的是
        N 个独立身份（不会因为「同邮箱重复注册」被封），
        而部署方只需维护**一套邮箱凭证**。

    ⚠️ `count > 1` 时必须配别名，否则 N 次注册会共用同一邮箱字面量 ——
       上游极易判定滥用，且收码会串台。这里直接拒绝，不做「偷偷跑」。
    """
    _admin_guard(request)
    if not settings.browser_enabled:
        raise HTTPException(503, detail={"error": {
            "message": "服务器未启用浏览器（BROWSER_ENABLED=false）"}})

    body = await request.json()
    explicit_email = str(body.get("email") or "").strip()
    try:
        count = int(body.get("count") or 1)
    except Exception:
        count = 1
    count = max(1, min(count, 50))          # 单次上限 50，防误操作打爆
    label_prefix = str(body.get("label") or "").strip()

    from .auth.login import login_with_browser
    from .auth import alias as alias_mod

    ident = None
    try:
        ident = alias_mod.from_settings(settings)
    except alias_mod.AliasError as e:
        if explicit_email:
            ident = None                    # 显式邮箱 → 不需要别名
        else:
            raise HTTPException(400, detail={"error": {
                "message": f"别名配置无效：{e}", "code": "alias_config_error"}})

    if count > 1 and ident is None:
        raise HTTPException(400, detail={"error": {
            "message": ("批量注册（count>1）必须先配置别名邮箱："
                        "在 .env 设置 MAIL_PRIMARY + ALIAS_SCHEME。"
                        "否则多个账号会共用同一地址，既触发上游风控，"
                        "也会导致收码串台。"),
            "code": "alias_required"}})
    if count > 1 and explicit_email:
        raise HTTPException(400, detail={"error": {
            "message": "批量注册时不要显式指定 email（应让系统派生别名）",
            "code": "invalid_request_error"}})

    # ── 规划本次要注册的地址列表 ─────────────────────────────
    if explicit_email:
        targets = [(explicit_email, "")]
    else:
        if ident is None:
            raise HTTPException(400, detail={"error": {
                "message": "email 必填（或配置 MAIL_PRIMARY 启用别名派生）",
                "type": "invalid_request_error"}})
        targets = []
        seen: set[str] = set()
        for i in range(count):
            seed = f"{label_prefix}-{i}" if label_prefix else None
            addr = ident.alias(alias_mod.make_tag(seed) if seed else None)
            while addr in seen:             # 极小概率撞车 → 重生成
                addr = ident.alias()
            seen.add(addr)
            targets.append((addr, label_prefix or ""))

    otp = get_backend()
    results: list[dict] = []

    # ⚠️ `login_with_browser` 用的是 Playwright **同步** API，与 asyncio
    #    事件循环互斥（在协程里直接调会抛 "Sync API inside the asyncio
    #    loop"）。本端点是 async，所以必须卸到线程池执行。
    #    —— 这是 v2 之前就存在的缺陷，单账号路径不易暴露；批量派生使
    #       它成为必经路径，故在此显式修正。
    import asyncio

    for addr, lbl in targets:
        stages: list[dict] = []

        def on_stage(st, detail="", _s=stages):
            _s.append({"stage": st, "detail": detail, "at": time.time()})

        try:
            res = await asyncio.to_thread(
                login_with_browser, addr, otp_provider=otp, on_stage=on_stage)
        except Exception as e:
            results.append({
                "email": addr, "ok": False,
                "error": f"浏览器阶段异常：{type(e).__name__}: {str(e)[:200]}",
                "stages": stages,
            })
            continue
        if not res.ok:
            results.append({
                "email": addr, "ok": False,
                "error": f"阶段 {res.stage}：{res.error}",
                "stages": stages,
            })
            continue

        client = AccioClient(cookies=dict(res.cookies))
        try:
            info = client.fetch_userinfo()
        except Exception as e:
            results.append({
                "email": addr, "ok": False,
                "error": f"注册后校验失败：{str(e)[:200]}",
                "stages": stages,
            })
            continue

        cred = Credential(
            user_id=str(info.get("userId") or ""),
            accio_id=str(info.get("accioId") or ""),
            nickname=str(info.get("nickname") or ""),
            email=str(info.get("email") or addr),
            cookies=dict(res.cookies),
        )
        if lbl:
            try:
                cred.label = lbl
            except Exception:
                pass                        # 旧版本无 label 字段则忽略
        _enrich(cred, client)
        pool.save(cred)
        results.append({"email": addr, "ok": True,
                        "account_key": cred.account_key,
                        "data": cred.to_public(), "stages": stages})

    ok_n = sum(1 for r in results if r["ok"])
    if count == 1:
        r0 = results[0]
        if not r0["ok"]:
            raise HTTPException(502, detail={"error": {
                "message": f"注册失败（{r0['error']}）",
                "code": "register_failed"}, "stages": r0["stages"]})
        return {"data": r0["data"], "message": "注册成功",
                "stages": r0["stages"]}

    # 批量：逐条回报成败，不用整体 500 掩盖部分失败
    return {
        "message": f"批量注册完成：成功 {ok_n}/{len(results)}",
        "total": len(results), "ok_count": ok_n,
        "results": results,
    }


@router.get("/admin/api/alias")
def alias_status(request: Request):
    """别名配置自检 —— 部署方上线前应先跑这个。

    ══════════════════════════════════════════════════════════
    响应契约（**稳定**：四个键始终存在，未配置时为 null / []）
    ══════════════════════════════════════════════════════════
        configured : bool                 是否启用了别名机制
        identity   : dict | null          身份详情，**不含任何密钥**
                                           {primary, scheme, alias_domain,
                                            tag_length,
                                            accept_primary_fallback,
                                            example_alias}
        samples    : list[str]            派生的示例地址（5 个），未配置时 []
        checks     : list[{ok,item,detail}] 自检结论，**始终至少一条**

    设计意图：别名配置有多个易错点（机制选错、域名写错、
    主地址与 IMAP 登录账号不一致），失败现象是「注册成功但永远
    等不到验证码」。与其让用户在注册里踩坑，不如给一个**零副作用**
    的诊断端点，一眼看出配置对不对。

    ⚠️ 契约稳定性说明：早期版本在 `configured=False` 时**直接省略**
       `identity` / `samples` 两个键，导致前端必须写 `d.identity?.xxx`
       这类防御式访问。现已统一为**键恒在、值为空**，前端可直接读取。
    """
    _admin_guard(request)
    from .auth import alias as alias_mod

    # ── 契约底座：四个键始终存在 ──────────────────────────────
    out: dict = {"configured": False, "identity": None,
                 "samples": [], "checks": []}

    try:
        ident = alias_mod.from_settings(settings)
    except alias_mod.AliasError as e:
        out["checks"].append({"ok": False, "item": "身份构造",
                              "detail": str(e)})
        return {"data": out}
    except Exception as e:
        out["checks"].append({"ok": False, "item": "配置解析",
                              "detail": f"{type(e).__name__}: {e}"})
        return {"data": out}

    if ident is None:
        out["checks"].append({
            "ok": False, "item": "别名",
            "detail": "未配置 MAIL_PRIMARY —— 当前为单邮箱模式，"
                      "批量注册会共用同一地址（易触发上游风控 + 收码串台）"})
        return {"data": out}

    out["configured"] = True
    out["identity"] = ident.describe()

    # ── 派生 5 个示例，并自检「互不串台」──────────────────────
    try:
        samples = [ident.alias_for_account(f"sample-{i}") for i in range(5)]
        out["samples"] = samples
        out["checks"].append({"ok": len(set(samples)) == 5, "item": "派生唯一性",
                              "detail": "5 个示例地址互不相同"})

        # 交叉匹配自检：每个地址只认自己
        cross_ok = all(
            ident.matches(s, s) and
            not any(ident.matches(s, other) for other in samples if other != s)
            for s in samples
        )
        out["checks"].append({
            "ok": cross_ok, "item": "归属隔离",
            "detail": "每个别名只认领投递给自己的邮件（防串台）" if cross_ok
                      else "🔴 存在串台风险，请检查 ALIAS_SCHEME / ALIAS_DOMAIN"})
    except Exception as e:
        out["checks"].append({"ok": False, "item": "派生自检",
                              "detail": f"{type(e).__name__}: {e}"})

    # ── 与收码后端的一致性 ───────────────────────────────────
    if settings.otp_backend == "imap":
        same = (settings.imap_user or "").strip().lower() == ident.primary.lower()
        out["checks"].append({
            "ok": same, "item": "IMAP 账号一致性",
            "detail": "IMAP_USER 与 MAIL_PRIMARY 一致" if same
                      else f"⚠️ IMAP_USER={settings.imap_user!r} 与 "
                           f"MAIL_PRIMARY={ident.primary!r} 不一致；"
                           "别名必须投递到 IMAP 登录的这个收件箱才收得到码"})
    elif settings.otp_backend == "cloudmail":
        ok = ident.scheme == "domain"
        out["checks"].append({
            "ok": ok, "item": "CloudMail 机制",
            "detail": "机制匹配" if ok
                      else "CloudMail 需 ALIAS_SCHEME=domain + ALIAS_DOMAIN"})
    else:
        out["checks"].append({
            "ok": True, "item": "收码后端",
            "detail": f"当前为 {settings.otp_backend}，别名归属由该后端自行处理"})

    return {"data": out}



@router.post("/admin/api/credentials/health")
async def credentials_health(request: Request):
    """**并发健康巡检** —— 逐个校验凭证 cookie 是否仍有效。

    body: {"account_keys": [...]}   留空 = 巡检全部

    实现要点：
      · 并发执行（线程池），而非串行 —— N 个账号串行会慢到不可用
      · 单个失败不影响整体（各自独立 try）
      · 结果**回写凭证状态**（mark_verified / mark_error），
        使巡检结论在凭证列表里持久可见

    返回逐条结论 + 汇总，便于前端直接渲染。
    """
    _admin_guard(request)
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    keys = body.get("account_keys") or []
    targets = ([pool.get(k) for k in keys] if keys else pool.all())
    targets = [c for c in targets if c is not None]

    if not targets:
        return {"data": {"total": 0, "healthy": 0, "unhealthy": 0,
                         "results": [],
                         "load_failures": [{"file": f, "reason": r}
                                           for f, r in pool.load_failures]}}

    import asyncio

    def _probe(cred):
        """单个凭证探活 —— 拉 userinfo，判定 cookie 是否有效。"""
        t0 = time.time()
        try:
            client = AccioClient(cookies=dict(cred.cookies))
            info = client.fetch_userinfo()
            if not info or not info.get("userId"):
                raise RuntimeError("返回空用户信息")

            # 顺带刷新余额/额度（复用既有 enrich 逻辑）
            try:
                _enrich(cred, client)
            except Exception:
                pass
            pool.mark_verified(cred.account_key, credits=cred.credits)
            return {
                "account_key": cred.account_key,
                "email": cred.email,
                "nickname": cred.nickname,
                "ok": True,
                "latency_ms": int((time.time() - t0) * 1000),
                "credits": cred.credits,
            }
        except Exception as e:
            msg = f"{type(e).__name__}: {str(e)[:180]}"
            pool.mark_error(cred.account_key, msg)
            return {
                "account_key": cred.account_key,
                "email": cred.email,
                "nickname": cred.nickname,
                "ok": False,
                "latency_ms": int((time.time() - t0) * 1000),
                "error": msg,
            }

    # 并发探活，但**限制并发度**防止对上游造成压力
    sem = asyncio.Semaphore(int(getattr(settings, "health_concurrency", 6)))

    async def _one(cred):
        async with sem:
            return await asyncio.to_thread(_probe, cred)

    results = list(await asyncio.gather(*(_one(c) for c in targets)))
    healthy = sum(1 for r in results if r["ok"])

    return {"data": {
        "total": len(results), "healthy": healthy,
        "unhealthy": len(results) - healthy,
        "checked_at": time.time(),
        "results": results,
        # 启动时解密失败的文件（通常 = SECRET_KEY 变更）。非空说明
        # 有账号没被载入 —— 必须让运维看见，而不是静默消失。
        "load_failures": [{"file": f, "reason": r}
                          for f, r in pool.load_failures],
    }}


@router.post("/admin/api/credentials/bulk")
async def credentials_bulk(request: Request):
    """**批量操作** —— 一次对多个凭证执行启用/禁用/删除。

    body: {"action": "enable|disable|delete", "account_keys": [...]}

    为什么需要：凭证池到几十个之后，逐个点按钮不可用。
    批量删除是**破坏性操作**，因此要求 action=delete 时
    必须显式传 `confirm: true`，防止误触。
    """
    _admin_guard(request)
    body = await request.json()
    action = str(body.get("action") or "").strip().lower()
    keys = body.get("account_keys") or []

    if action not in ("enable", "disable", "delete"):
        raise HTTPException(400, detail={"error": {
            "message": "action 必须是 enable / disable / delete"}})
    if not keys:
        raise HTTPException(400, detail={"error": {
            "message": "account_keys 不能为空"}})
    if action == "delete" and not body.get("confirm"):
        raise HTTPException(400, detail={"error": {
            "message": "批量删除需显式 confirm:true（破坏性操作）",
            "code": "confirm_required"}})

    done, failed = [], []
    for k in keys:
        try:
            if action == "delete":
                ok = pool.delete(k)
            else:
                ok = pool.set_enabled(k, action == "enable")
            (done if ok else failed).append(k)
        except Exception as e:
            failed.append({"account_key": k, "error": str(e)[:150]})

    return {"data": {"action": action, "done": len(done),
                     "failed": len(failed),
                     "done_keys": done, "failed_keys": failed}}


# ── 审计 ────────────────────────────────────────────────────
@router.get("/admin/api/audit")
def audit_log(request: Request, limit: int = 100, ok: str = "",
              model: str = "", caller: str = ""):
    """调用审计查询。

    query: limit / ok(true|false) / model / caller
    """
    _admin_guard(request)
    from .core.audit import audit as _a

    okf = None
    if ok.lower() in ("true", "1", "yes"):
        okf = True
    elif ok.lower() in ("false", "0", "no"):
        okf = False

    rows = _a.recent(limit=max(1, min(limit, 500)), ok=okf,
                     model=model, caller=caller)
    return {"data": {"count": len(rows), "entries": rows,
                     "body_logged": _a.log_body,
                     "days_available": _a.days()}}


@router.get("/admin/api/audit/stats")
def audit_stats(request: Request, days: int = 7):
    """审计聚合统计 —— 仪表盘数据源。"""
    _admin_guard(request)
    from .core.audit import audit as _a
    return {"data": _a.stats(days=max(1, min(days, 90)))}


@router.post("/admin/api/audit/purge")
def audit_purge(request: Request):
    """清理超出保留期的审计分片。"""
    _admin_guard(request)
    from .core.audit import audit as _a
    n = _a.purge()
    return {"data": {"removed_days": n,
                     "retention_days": _a.retention_days}}


@router.get("/admin/api/dashboard")
def dashboard(request: Request, days: int = 7):
    """**综合仪表盘** —— 一屏汇总运行态。

    聚合四类信息：
      · 资源：凭证数 / 在线率 / 代理出口数 / 别名是否配置
      · 流量：调用量 / 成功率 / token 消耗 / 延迟分位
      · 健康：凭证错误分布 / 上游连通性标记
      · 配置：OTP 后端 / 浏览器 / 审计开关

    设计意图：把「散在多个 tab 的状态」压成一个端点，
    前端一屏展示，运维者不需要来回切换。
    """
    _admin_guard(request)
    from .core.audit import audit as _a
    from .core.proxy import pool as px_pool
    from .auth import alias as alias_mod

    creds = pool.all()
    enabled = [c for c in creds if c.enabled]
    errored = [c for c in creds if c.last_error]

    alias_ok, alias_info = False, None
    try:
        ident = alias_mod.from_settings(settings)
        if ident is not None:
            alias_ok = True
            alias_info = ident.describe()
    except Exception:
        pass

    stats = _a.stats(days=max(1, min(days, 90)))
    # 仪表盘不需要逐日明细以外的重字段，控制响应体积
    stats.pop("days_available", None)

    return {"data": {
        "resources": {
            "credentials_total": len(creds),
            "credentials_enabled": len(enabled),
            "credentials_error": len(errored),
            "proxy_count": len(px_pool.all()),
            "proxy_enabled": px_pool.enabled,
            "alias_configured": alias_ok,
            "alias": alias_info,
        },
        "traffic": stats,
        "health": {
            "error_credentials": [
                {"account_key": c.account_key, "email": c.email,
                 "error": str(c.last_error)[:160]}
                for c in errored[:10]
            ],
            "credentials": [c.to_public() for c in creds[:50]],
        },
        "config": {
            "otp_backend": settings.otp_backend,
            "browser_enabled": settings.browser_enabled,
            "proxy_strategy": settings.proxy_strategy,
            "audit_enabled": _a.enabled,
            "audit_log_body": _a.log_body,
            "rate_limit_rpm": getattr(settings, "global_rpm_limit", 0),
        },
        "generated_at": time.time(),
    }}


# ── API 调试台 ──────────────────────────────────────────────
@router.post("/admin/api/playground")
async def playground(request: Request):
    """**网页内 API 调试台** —— 直接测 /v1/chat/completions。

    body: {"prompt": "...", "model": "auto", "stream": false}

    存在意义：验证部署是否成功时，不必让用户去装 curl/python，
    也不用去理解 Bearer 头怎么填 —— 网页上点一下就知道通不通。

    与真实 /v1 请求走**同一套上游链路**（同凭证选择、同超时），
    因此验证结果可信。返回时附带延迟与 token 用量。
    """
    _admin_guard(request)
    body = await request.json()
    prompt = str(body.get("prompt") or "").strip()
    model = str(body.get("model") or "auto")
    if not prompt:
        raise HTTPException(400, detail={"error": {
            "message": "prompt 必填"}})

    from .openai_api import _pick_credential
    from .upstream.client import finalize_to_usage
    cred = _pick_credential()
    t0 = time.time()
    text, usage, err = "", None, ""
    try:
        client = AccioClient(cookies=dict(cred.cookies))
        for kind, obj in client.stream_query(
                prompt, model=model, language="zh"):
            if kind == "delta":
                p = obj.get("payload") or {}
                text += p.get("contentDelta") or ""
            elif kind == "finalize":
                usage = finalize_to_usage(obj)
                fc = (obj.get("payload") or {}).get("finalContent")
                if fc and len(fc) > len(text):
                    text = fc
            elif kind == "err":
                err = json.dumps(obj, ensure_ascii=False)[:300]
                break
    except Exception as e:
        err = f"{type(e).__name__}: {str(e)[:250]}"

    latency = int((time.time() - t0) * 1000)

    # 调试台的调用同样计入审计（kind=playground 便于区分）
    from .core.audit import audit as _a
    _a.record(kind="playground", model=model, caller="admin-console",
              account_key=cred.account_key, status=502 if (err and not text) else 200,
              ok=not (err and not text), latency_ms=latency,
              prompt_tokens=(usage or {}).get("prompt_tokens", 0),
              completion_tokens=(usage or {}).get("completion_tokens", 0),
              error=err, user_text=prompt)

    return {"data": {
        "ok": not (err and not text),
        "text": text,
        "usage": usage,
        "latency_ms": latency,
        "model": model,
        "account_key": cred.account_key,
        "error": err or None,
    }}


@router.post("/admin/api/playground/stream")
async def playground_stream(request: Request):
    """**流式调试台** —— SSE 实时吐出上游增量。

    与非流式版本共用同一上游链路，区别只在**边收边发**：
    运维者能立刻看到首个 token 何时到达（TTFT），
    这对判断上游是否健康比总耗时更有价值。

    事件格式（对齐 OpenAI 流式协议，便于复制到其它客户端）：
        data: {"choices":[{"delta":{"content":"..."}}]}
        data: {... ,"usage":{...}}      ← 末帧带用量
        data: [DONE]
    """
    _admin_guard(request)
    body = await request.json()
    prompt = str(body.get("prompt") or "").strip()
    model = str(body.get("model") or "auto")
    if not prompt:
        raise HTTPException(400, detail={"error": {"message": "prompt 必填"}})

    from .openai_api import _pick_credential
    from .core.audit import audit as _a
    from .upstream.client import finalize_to_usage
    cred = _pick_credential()

    def gen():
        t0 = time.time()
        text, usage, err, first_at = "", None, "", 0
        try:
            client = AccioClient(cookies=dict(cred.cookies),
                                 agent_id=cred.agent_id,
                                 project_path=cred.project_path,
                                 account_key=cred.account_key)
            for kind, obj in client.stream_query(prompt, model=model,
                                                 language="zh"):
                if kind == "delta":
                    p = obj.get("payload") or {}
                    dl = p.get("contentDelta") or ""
                    if dl:
                        if not first_at:
                            first_at = int((time.time() - t0) * 1000)
                        text += dl
                        yield "data: " + json.dumps(
                            {"choices": [{"index": 0,
                                          "delta": {"content": dl},
                                          "finish_reason": None}]},
                            ensure_ascii=False) + "\n\n"
                elif kind == "finalize":
                    usage = finalize_to_usage(obj)
                    fc = (obj.get("payload") or {}).get("finalContent")
                    if fc and fc != text:
                        # ── 终稿校准 ────────────────────────────
                        # 上游的 `contentDelta` 是**增量流**，实测存在
                        # 重复推送（同一字被推两次），直接拼接会得到
                        # "五五" 之类的结果；而 `finalContent` 是无重复
                        # 的权威终稿。
                        #
                        # 因此发一个 `x_final` 帧携带完整文本，由前端
                        # **整体覆盖**渲染 —— 既保留流式的实时性，
                        # 又保证最终结果与上游一致。
                        text = fc
                        yield "data: " + json.dumps(
                            {"choices": [{"index": 0, "delta": {},
                                          "finish_reason": None}],
                             "x_final": fc},
                            ensure_ascii=False) + "\n\n"
                elif kind == "err":
                    err = json.dumps(obj, ensure_ascii=False)[:400]
                    break
        except Exception as e:
            err = f"{type(e).__name__}: {str(e)[:300]}"

        latency = int((time.time() - t0) * 1000)
        if err:
            yield "data: " + json.dumps(
                {"error": {"message": err, "type": "upstream_error"}},
                ensure_ascii=False) + "\n\n"

        # 末帧：用量 + 收尾信号
        done_frame = {"choices": [{"index": 0, "delta": {},
                                   "finish_reason": "stop" if not err else None}]}
        if usage:
            done_frame["usage"] = usage
        yield "data: " + json.dumps(done_frame, ensure_ascii=False) + "\n\n"
        yield "data: [DONE]\n\n"

        _a.record(kind="playground", model=model, caller="admin-console",
                  account_key=cred.account_key,
                  status=502 if err else 200, ok=not err,
                  latency_ms=latency,
                  prompt_tokens=(usage or {}).get("prompt_tokens", 0),
                  completion_tokens=(usage or {}).get("completion_tokens", 0),
                  error=err, stream=True, user_text=prompt,
                  extra={"ttft_ms": first_at})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ── 代理池 / IP 轮换 ────────────────────────────────────────
@router.get("/admin/api/proxies")
def list_proxies(request: Request, probe: int = 0):
    """列出代理出口与健康度。`probe=1` 会实测每个出口的 IP（较慢）。"""
    _admin_guard(request)
    from .core.proxy import pool as px_pool
    if probe:
        items = px_pool.probe_all()
    else:
        items = [e.to_public() for e in px_pool.all()]
    return {
        "data": {
            "strategy": settings.proxy_strategy,
            "enabled": px_pool.enabled,
            "mihomo_api": settings.mihomo_api,
            "mihomo_group": settings.mihomo_group,
            "proxies": items,
        }
    }


@router.get("/admin/api/proxies/groups")
def proxy_groups(request: Request):
    """列出 mihomo 可切换的策略组与节点（支持「同端口换 IP」）。"""
    _admin_guard(request)
    from .core.proxy import pool as px_pool
    return {"data": px_pool.mihomo_groups()}


@router.post("/admin/api/proxies/switch")
async def proxy_switch(request: Request):
    """切换 mihomo 策略组的当前节点 → 出口 IP 随之改变。

    body: {"group": "<group>", "node": "<完整节点名>"}
    """
    _admin_guard(request)
    from .core.proxy import pool as px_pool, current_ip, proxies_for
    body = await request.json()
    group = str(body.get("group") or settings.mihomo_group)
    node = str(body.get("node") or "")
    if not node:
        raise HTTPException(400, detail={"error": {"message": "node 必填"}})
    before = px_pool.mihomo_probe(group)
    ok, after = px_pool.mihomo_switch(group, node, verify=True)
    if not ok:
        raise HTTPException(502, detail={"error": {
            "message": f"切换失败（group={group} node={node}）—— "
                       f"请确认 node 属于该组；listeners 固定槽位不可切换"}})
    return {"message": f"已切换到 {node}", "current_ip": after or before,
            "ip_before": before, "ip_changed": bool(after and after != before)}


@router.post("/admin/api/proxies/rotate")
async def proxy_rotate(request: Request):
    """在当前策略组内轮换到下一个节点，返回新节点与出口 IP。"""
    _admin_guard(request)
    from .core.proxy import pool as px_pool, current_ip, proxies_for
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    group = str(body.get("group") or settings.mihomo_group)
    before = px_pool.mihomo_probe(group)
    nxt, after = px_pool.mihomo_rotate(group)
    if not nxt:
        raise HTTPException(502, detail={"error": {
            "message": f"轮换失败（group={group}）—— 若该组是 listeners 固定槽位"
                       f"则不可切换；请用 proxy-groups 里的组"}})
    return {"message": f"已轮换到 {nxt}", "node": nxt,
            "current_ip": after or before, "ip_before": before,
            "ip_changed": bool(after and after != before)}


@router.get("/admin/api/proxies/current-ip")
def proxy_current_ip(request: Request, mode: str = "mihomo"):
    """当前出口 IP。

    `mode=mihomo`（默认）→ 探测 **mihomo 主端口**，
        这是被策略组切换影响的那条链路 —— 验证轮换必须看它。
    `mode=pool` → 探测凭证拿到的 sticky 池出口（随机一个）。
    `mode=direct` → 探测本机直连出口（不走代理）。

    ⚠ 两者出口不同是正常的：mihomo 主端口受 AUTO/ROTATE 组控制，
      而固定槽位（listeners）绑死节点、切不动。
    """
    _admin_guard(request)
    from .core.proxy import current_ip, proxies_for, pool as px_pool
    if mode == "direct":
        return {"data": {"ip": current_ip(None), "proxy": "direct",
                         "mode": mode, "strategy": settings.proxy_strategy}}
    if mode == "pool":
        px, entry = proxies_for("")
        return {"data": {"ip": current_ip(px),
                         "proxy": entry.url if entry else "direct",
                         "mode": mode, "strategy": settings.proxy_strategy}}
    # 默认 mihomo 主端口
    murl = px_pool.mihomo_proxy_url()
    mpx = {"http": murl, "https": murl} if murl else None
    return {"data": {"ip": current_ip(mpx), "proxy": murl or "direct",
                     "mode": "mihomo", "strategy": settings.proxy_strategy}}


# ── 概览 ────────────────────────────────────────────────────
@router.get("/admin/api/overview")
def overview(request: Request):
    _admin_guard(request)
    from .core.proxy import pool as px_pool
    creds = pool.all()
    return {
        "data": {
            "credential_total": len(creds),
            "credential_enabled": len([c for c in creds if c.enabled]),
            "credential_error": len([c for c in creds if c.last_error]),
            "login_sessions": len(web_login.all()),
            "otp_backend": settings.otp_backend,
            "browser_enabled": settings.browser_enabled,
            "proxy_enabled": px_pool.enabled,
            "proxy_count": len(px_pool.all()),
            "proxy_strategy": settings.proxy_strategy,
        }
    }
