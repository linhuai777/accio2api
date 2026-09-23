# 代理池 / IP 轮换 — 部署与原理

> 2026-09-23 实装。解决「同一 IP 高频登录/注册被 Accio 风控」的问题。

---

## 一、为什么需要

Accio（阿里系）对**同 IP 的密集注册/登录**有风控：

- 触发 Baxia 滑块挑战升级（从"无感"变"必须拖动"）
- 验证码下发频率提高甚至静默丢弃
- 同 IP 多账号可能被判**关联**，一起封

给每个凭证绑独立出口 IP 可以：分散压力、绕开"该 IP 已被挑战过"的状态、
降低异常评分。**但要注意：同一账号的出口必须稳定**，频繁跳 IP 反而会触发
「会话异常」风控 —— 这就是默认策略是 `sticky` 而不是 `rotate` 的原因。

---

## 二、三条不同的链路（最容易踩的坑）

```
                    ┌─────────────────────────────────┐
   mihomo 主端口     │ <port> (mixed-port)             │  ← 出口由策略组决定
   (mixed-port)     │ rules: MATCH,<group>            │     ✅ 可被 API 切换
                    └─────────────────────────────────┘
                    ┌─────────────────────────────────┐
   listeners 固定槽位 │ <port-a> → <节点A>  绑死         │  ← 出口固定
   (slots)          │ <port-b> → <节点B>  绑死         │     ❌ 切不动
                    │ <port-c> → <节点C>  绑死         │
                    └─────────────────────────────────┘
```

**结论**：

| 需求 | 用哪个 |
|---|---|
| 要**多个不同 IP 同时存在** | 固定槽位（listeners，一端口一节点），一个端口一个 IP |
| 要**动态更换 IP**（同端口） | mihomo 主端口 + 切 `proxy-groups` 里的组 |

`MIHOMO_GROUP` **必须填 proxy-groups 里的组名**（如 `AUTO` / `ROTATE`，
具体名字由你的 mihomo 配置决定），
填成 `GLOBAL` 或 listeners 槽位名会返回 HTTP 200 但 IP 纹丝不动 —— 这是本项目
开发时踩过的最大的坑，已在代码里加了显式警告与 404 提示。

---

## 三、配置

`.env`：

```ini
# 静态出口（逗号分隔）—— 每个凭证按 sticky 分到一个
PROXIES=http://127.0.0.1:<port-a>,http://127.0.0.1:<port-b>,http://127.0.0.1:<port-c>

# sticky（默认，推荐）| rotate | random
PROXY_STRATEGY=sticky

# mihomo 控制 API —— 启用后可在 UI 里动态换 IP
MIHOMO_API=http://127.0.0.1:<controller-port>
MIHOMO_GROUP=<group>          # 例：AUTO —— 必须是 proxy-groups 里的组名

MIHOMO_PROXY_URL=            # 留空 = 取 PROXIES 第一项
PROXY_CHECK_URL=https://api.ipify.org
```

---

## 四、三种策略

| 策略 | 行为 | 适用 |
|---|---|---|
| `sticky`（默认） | 同凭证永远同出口；不同凭证尽量分散 | **生产推荐**。IP 稳，风控不报警 |
| `rotate` | 每次请求换下一个出口 | 短时高频、不需要会话连续性 |
| `random` | 每次随机 | 调试 |

`sticky` 的分配不是简单取模 —— 取模会碰撞（两个不同账号可能落到同一出口）。
实现改用**最少占用优先 + 哈希决定同分先后**：既稳定（同 key 恒定），
又均匀（账号铺满所有出口再叠加）。实测 20 账号分布 3/3/3/2/3/3/3。

---

## 五、API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/admin/api/proxies` | 列出出口。`?probe=1` 实测每个出口 IP |
| GET | `/admin/api/proxies/groups` | 列出可切换的策略组与节点 |
| POST | `/admin/api/proxies/switch` | `{"group":"<group>","node":"<完整节点名>"}` 切节点 |
| POST | `/admin/api/proxies/rotate` | 组内轮换到下一个节点 |
| GET | `/admin/api/proxies/current-ip` | 探测出口。`?mode=mihomo\|pool\|direct` |

`switch` / `rotate` 返回 `ip_before` / `current_ip` / `ip_changed`，
**切换必然伴随最长 ~18 秒的等待**（内核已建立的连接会复用，早期返回会
得到"没变"的假结论）。

---

## 六、实测记录（2026-09-23）

```
出口实测：
  ✅ <port-a>  → <ip-a>        ✅ <port-c>  → <ip-c>
  ✅ <port-b>  → <ip-b>        ✅ <port-d>  → <ip-d>

动态切换（策略组）：
  → <node-a>   <ip-x> → <ip-y>   ✅
  → <node-b>   <ip-y> → <ip-x>   ✅

sticky 均匀性：N 个账号均匀铺满可用出口（无碰撞）
sticky 稳定性：同 key 连续调用恒定 ✅
```

> 具体端口、出口 IP、节点名属**部署方私有基础设施信息**，不进仓库。
> 部署后用 UI 的「探测」按钮查看自己的出口。

---

## 七、注意事项

1. **WebSocket 必须与 HTTP 同出口**。前端 HTTP 探测走 A、WS 连接走 B
   会被判「会话劫持」。代码里两处都取了同一个 `account_key` 的代理。
2. **节点名要完整精确**。必须原样照抄（含 emoji 与分隔符，例如
   含地区 emoji 与全称）；从 `/proxies/groups` 的返回里复制完整名称。
   的返回里复制完整名称。
3. **出口失败会自动摘除**。连续失败 3 次进入退避（30s × 失败次数，上限 5 分钟），
   恢复后自动重新参与分配。
4. **`public_ip` 与账号 `routeRegion` 不一致会提高异常评分**。有条件时
   让出口地区匹配账号区域。
