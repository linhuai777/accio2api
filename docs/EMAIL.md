# 邮箱配置指南

本文档说明 accio2api 支持的收码方式、**别名邮箱机制**的原理与配置，
以及在 Gmail / Outlook 上的具体操作步骤。

---

## 目录

1. [为什么需要别名邮箱](#1-为什么需要别名邮箱)
2. [两种别名机制](#2-两种别名机制)
3. [收码后端选型](#3-收码后端选型)
4. [Gmail 配置步骤](#4-gmail-配置步骤)
5. [Outlook 配置步骤](#5-outlook-配置步骤)
6. [自建域名（CloudMail）配置](#6-自建域名cloudmail配置)
7. [配置自检](#7-配置自检)
8. [常见故障排查](#8-常见故障排查)
9. [实现细节与设计取舍](#9-实现细节与设计取舍)

---

## 1. 为什么需要别名邮箱

批量注册时，如果**所有账号共用同一个邮箱字面量**，会遇到两类问题：

### 1.1 上游风控

服务端通常把「邮箱」当作账号的自然主键之一。同一邮箱重复注册会被
判定为异常行为 —— 轻则拒绝，重则把该邮箱加入黑名单。**单一邮箱
等于单一身份**，注册第二个账号就撞墙。

### 1.2 收码串台

多个注册流程并行时，若共用一个收件箱，就无法区分
「这封验证码是发给哪个账号的」。结果是账号 A 的会话里存进了
账号 B 的验证码 —— **流程全部成功，没有任何报错**，
只有事后登录时才发现进错了号。这类故障排查成本极高。

**别名邮箱同时解决两者**：每个账号一个独立地址（上游看到 N 个身份），
但**全部投递到同一个物理收件箱**（我们只需维护一套邮箱凭证）。

```
                    ┌─────────────────────────────┐
   别名 A ─────────▶│                             │
   yourname+a@gmail  │   主收件箱（IMAP 登录此账号）  │
                    │   yourname@gmail.com         │
   别名 B ─────────▶│                             │
   yourname+b@gmail  │   全部别名投递到这里           │
                    └─────────────────────────────┘
```

---

## 2. 两种别名机制

主流邮箱的别名机制分两派，本项目的 `ALIAS_SCHEME` 对应选择：

| 机制 | 值 | 形态 | 支持者 |
|---|---|---|---|
| **`+` 子地址** | `plus` | 主名`+标签`@原域名<br>`yourname+accio01@gmail.com` | Gmail、Outlook、iCloud、<br>Proton、Yahoo、QQ、163 |
| **域名别名** | `domain` | 任意本地名@自有域名<br>`any-name@yourdomain.com` | 自建域名 + 泛域名收信 |

### 2.1 `+` 子地址（RFC 5233）

`+` 子地址是**标准行为**，不是某家服务商的私有特性：

- **RFC 5321** 规定 `+` 是合法的邮箱本地部分字符
- **RFC 5233**（Subaddressing）规定：投递到 `user+tag@domain` 的邮件
  必须进入 `user@domain` 的收件箱

所以 Gmail / Outlook / iCloud / Proton 等都遵循此规则。

**限制**：标签通常是**单向**的 —— 上游看到的是不同地址，但你
**不能用别名发信**（发件人还是主地址）。对「注册收码」场景无影响。

### 2.2 域名别名

自有域名 + 泛域名（catch-all）收信时，任意 `本地名@你的域名`
都会进同一个收件箱。自由度最高（本地名可任意构造），
但需要自己拥有一个域名并配置 MX 记录。

CloudMail 自建服务天然属于此类。

### 2.3 机制自动判定

`ALIAS_SCHEME=auto`（默认）时，系统按域名判定：

- 已知的 `+` 子地址服务商域名 → `plus`
- 其它域名 → **拒绝猜测**，要求显式配置

> **为什么不一猜了之**：机制选错的失败现象是
> 「注册流程成功，但永远等不到验证码」——
> 现象安静且难以归因。宁可启动时报错要求显式配置。

---

## 3. 收码后端选型

| 后端 | 值 | 适用场景 | 别名支持 |
|---|---|---|---|
| **IMAP** | `imap` | 任意标准邮箱（最通用） | ✅ `plus` / `domain` |
| **CloudMail** | `cloudmail` | 自建收发信服务 | ✅ `domain`（泛域名） |
| **手动** | `manual` | 网页上人工输入（零邮箱配置） | — |

```bash
# .env
OTP_BACKEND=imap            # 或 cloudmail / manual
```

> **推荐**：个人使用选 `imap` + Gmail/Outlook 应用专用密码，
> 十分钟配好，无需自建服务。

---

## 4. Gmail 配置步骤

Gmail **已禁用「账号密码直连 IMAP」**，必须使用**应用专用密码**。

### 4.1 开启两步验证（前提）

应用专用密码仅在已开启两步验证时可用。

1. 访问 <https://myaccount.google.com/security>
2. 「两步验证」→ 按引导完成开启

### 4.2 生成应用专用密码

1. 访问 <https://myaccount.google.com/apppasswords>
2. 应用名称填任意值（如 `accio2api`）→ 点击「创建」
3. 复制生成的 **16 位密码**（形如 `abcd efgh ijkl mnop`）

> ⚠️ 该密码**只显示一次**，关闭页面后无法再查看，需重新生成。
> ⚠️ 应用专用密码 = **完整邮箱访问权限**，按密钥对待：
> 不要提交到仓库、不要贴进聊天记录。

### 4.3 填写配置

```bash
# .env
OTP_BACKEND=imap
IMAP_HOST=imap.gmail.com
IMAP_PORT=993
IMAP_USER=yourname@gmail.com          # 主地址（收件箱）
IMAP_PASSWORD=abcd efgh ijkl mnop    # 应用专用密码（16 位）

# 别名：Gmail 属 plus 机制
MAIL_PRIMARY=yourname@gmail.com
ALIAS_SCHEME=plus
```

### 4.4 Gmail 的额外说明

- **点号不区分**：`your.name@gmail.com` 与 `yourname@gmail.com`
  是同一收件箱。本项目已处理该折算。
- **`googlemail.com` 等价**：`yourname@googlemail.com` 与
  `yourname@gmail.com` 同一收件箱，已处理。
- **IMAP 搜索限制**：Gmail 的 IMAP `SEARCH` **不索引 `+标签`**，
  搜 `To: yourname+accio01@gmail.com` 通常无结果。
  本项目因此采用「按发件人/时间拉候选 → 客户端解析 To 头判定归属」
  的方式，而非依赖服务端搜索。

---

## 5. Outlook 配置步骤

Outlook.com / Hotmail / Live 同样需要应用专用密码。

### 5.1 开启两步验证

1. 访问 <https://account.microsoft.com/security>
2. 「高级安全选项」→ 开启「双重验证」

### 5.2 生成应用专用密码

1. 同一页面下方「应用密码」→「创建新的应用密码」
2. 复制生成的密码

> 若界面无「应用密码」入口，说明该账号**未被强制要求**
> 两步验证 —— 此时（且仅此时）可直接使用账号主密码登录 IMAP。

### 5.3 填写配置

```bash
# .env
OTP_BACKEND=imap
IMAP_HOST=outlook.office365.com       # 注意不是 imap.outlook.com
IMAP_PORT=993
IMAP_USER=yourname@outlook.com
IMAP_PASSWORD=<应用专用密码>

MAIL_PRIMARY=yourname@outlook.com
ALIAS_SCHEME=plus
```

### 5.4 Outlook 的额外说明

- 服务器地址是 **`outlook.office365.com`**（常见错误是填
  `imap.outlook.com`，该地址无法连接）
- **点号同样不区分**（`first.last@outlook.com` == `firstlast@outlook.com`），
  已处理
- Hotmail / Live 与 Outlook 同属一套基础设施，配置相同

---

## 6. 自建域名（CloudMail）配置

自有域名时使用 `domain` 机制，自由度最高。

### 6.1 前置条件：泛域名收信

在域名 DNS 设置中配置 MX 记录指向邮件服务器，
并确保服务端开启 **catch-all（泛收信）** ——
即「所有本地名 @ 该域名」都进同一收件箱。

### 6.2 填写配置

```bash
# .env
OTP_BACKEND=cloudmail
CLOUDMAIL_BASE=https://mail.yourdomain.com
CLOUDMAIL_EMAIL=admin@yourdomain.com
CLOUDMAIL_PASSWORD=<登录密码>
CLOUDMAIL_TOKEN=<裸 token，可选；过期会自动用密码重新获取>

# 别名：自有域名走 domain 机制
MAIL_PRIMARY=admin@yourdomain.com
ALIAS_SCHEME=domain
ALIAS_DOMAIN=mail.yourdomain.com      # 别名使用的域名
```

### 6.3 CloudMail 的实测要点

以下均为实际调试中确认的行为，配置时需注意：

- 鉴权使用**裸 token**（`Authorization: <token>`），
  加 `Bearer ` 前缀会返回 401
- `emailList` 接口**忽略 `email` 参数**，返回全表最新 N 条，
  必须**客户端按 `toEmail` 过滤**
- 邮件 `content` 可能是数十 KB 的 HTML，请求需限制响应大小
- token 会过期；401 时用 `email` + `password` 重新 `genToken` 可自愈

---

## 7. 配置自检

配好之后**先跑自检端点**，不要直接去注册：

```bash
curl -H "Authorization: Bearer <ADMIN_KEY>" \
     http://127.0.0.1:8000/admin/api/alias
```

返回示例（配置正确时）：

```json
{
  "data": {
    "configured": true,
    "identity": {
      "primary": "yourname@gmail.com",
      "scheme": "plus",
      "tag_length": 6,
      "example_alias": "yourname+example@gmail.com"
    },
    "samples": [
      "yourname+a5bb10@gmail.com",
      "yourname+0899cd@gmail.com",
      "yourname+2be9fe@gmail.com"
    ],
    "checks": [
      {"ok": true, "item": "派生唯一性", "detail": "5 个示例地址互不相同"},
      {"ok": true, "item": "归属隔离",  "detail": "每个别名只认领投递给自己的邮件"},
      {"ok": true, "item": "IMAP 账号一致性", "detail": "IMAP_USER 与 MAIL_PRIMARY 一致"}
    ]
  }
}
```

`checks` 中任意一项 `ok: false` 都应先修正再注册。

---

## 8. 常见故障排查

| 现象 | 可能原因 | 处理 |
|---|---|---|
| 注册成功但**永远等不到验证码** | ① 机制选错（该用 `domain` 却用了 `plus`）<br>② `IMAP_USER` 与 `MAIL_PRIMARY` 不一致<br>③ 别名域名未配 catch-all | 跑 `/admin/api/alias` 自检；核对三项配置 |
| IMAP 连接失败 | 用了账号密码而非应用专用密码 | 生成应用专用密码（见 §4.2 / §5.2） |
| Outlook 连不上 | 服务器地址写成 `imap.outlook.com` | 改为 `outlook.office365.com` |
| 「别名配置无效」启动报错 | `ALIAS_SCHEME=auto` 但域名无法判定 | 显式设置 `ALIAS_SCHEME=plus` 或 `domain` |
| 账号 A 收到账号 B 的验证码 | 别名隔离失效（**不应发生**） | 立即停止批量任务，跑 `tests/test_alias.py` 与 `tests/test_otp_routing.py` |
| 批量注册被拒（`alias_required`） | `count>1` 但未配别名 | 配置 `MAIL_PRIMARY` + `ALIAS_SCHEME` |

---

## 9. 实现细节与设计取舍

本项目的别名引擎位于 `app/auth/alias.py`，归属判定在
`app/auth/otp.py:_belongs()`。以下几个设计决定值得说明。

### 9.1 归属判定必须精确，不能靠子串

**反面案例**：若用「标签是否为收件人头的子串」判定，则
标签 `a` 会命中 `yourname+xyzabc@gmail.com` ——
账号 A 就会拿到账号 B 的验证码。

**本项目的做法**：从收件人头中**正则解析出完整地址**，
再按「同一收件箱 + 标签完全相等」判定。
判定逻辑拆成 `_same_mailbox()` / `_label()` / `_is_primary()`
三个可独立测试的谓词。

### 9.2 收件人头要读全，不能只看 `To`

部分 MTA 在最终投递时会**改写 `To` 为主地址**，
但 `Delivered-To` / `X-Original-To` / `Envelope-To` 中
**仍保留原始的别名地址**。因此 `_recipients()` 会读取全部
五个头字段作为判定依据。

### 9.3 点号折算按域名区分

- Gmail / Outlook 系：点号**不区分**（`a.b@` == `ab@`）
- QQ / 163 / 通用域名：点号是**有效字符**，不折算

这是服务商行为差异，不是通用规则，故在
`DOT_INSENSITIVE_DOMAINS` 中显式列举。

### 9.4 验证码提取使用词边界

正则 `\b\d{6}\b` 而非 `\d{6}` —— 否则 `12345678`
会被截出 `123456`，返回一个**错误的验证码**，
导致登录失败且难以定位。

### 9.5 标签生成支持可复现

`make_tag(seed)` 对同一 seed 生成同一标签，
使「从别名反查账号」成为可能，便于排查。
未提供 seed 时使用 `secrets` 生成不可预测的标签，
避免上游按标签规律识别批量注册。

### 9.6 显式拒绝而非静默降级

以下情形直接报错，不做「尽力而为」的降级：

- `count > 1` 且未配别名 → `alias_required`
- `ALIAS_SCHEME=auto` 但域名不可判定 → 启动报错
- 别名标签含非安全字符 → 抛 `AliasError`

理由：这些配置错误若被静默容忍，会以「收码失败」的形式
在**很久之后**才暴露，且现象与原因相距甚远。

---

## 附：相关测试

```bash
python3 tests/test_alias.py         # 别名引擎（机制/派生/归属判定）
python3 tests/test_otp_routing.py   # OTP 归属判定（并行隔离）
```

两组测试均零依赖、无需真实邮箱，可直接运行。
