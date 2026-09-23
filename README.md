<div align="center">

# accio2api

**把你的 Accio Work 账号，变成一个 OpenAI 兼容 API**

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11+-green.svg)](https://www.python.org/)

</div>

```text
你的客户端 ──OpenAI 协议──▶ accio2api ──Accio 协议──▶ Accio Work
(Cherry Studio / New API)    (本项目)                  (你自己的账号)
```

接入后，任何支持 OpenAI 协议的客户端都能直接用你的 Accio 账号对话。

---

## 30 秒跑起来

```bash
git clone https://github.com/linhuai777/accio2api.git && cd accio2api

python setup.py            # ← 交互式配置向导，一步步问，自动生成 .env
docker compose up -d
```

向导会问端口、密钥、取码方式等**必须你决定**的几项，其余用验证过的默认值填好。
拉镜像、启动容器这类耗时步骤只显示动态状态，不刷屏；出错时才回放**关键几行**
（自动滤掉进度噪声），需要你决策的内容才会详细展开。

不想回答任何问题的话：`python setup.py --yes` 全默认，`--start` 配置完直接启动。

<details>
<summary><b>手动配置（不想用向导）</b></summary>

```bash
cp .env.example .env && vim .env        # 至少设置 API_KEY
docker compose up -d
```

`.env.example` 里每一项都有注释，见 [配置文件](#配置) 一节。

</details>

验证：

```bash
curl http://localhost:8000/health
# {"status":"ok","credentials":0,...}   ← credentials:0 正常，还没加账号

curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d '{"model":"deepseek-v4.1","messages":[{"role":"user","content":"hi"}]}'
```

打开 `http://localhost:8000/admin`，用 `data/.admin_key` 里的密钥登录，然后加账号。

> 首次启动会自动生成 `ADMIN_KEY` 与 `SECRET_KEY` 存到 `data/`（权限 0600）。
> **`SECRET_KEY` 决定凭证能否解密 —— 务必备份，丢了已存账号全部不可恢复。**

<details>
<summary><b>裸机运行 / 不用 Docker</b></summary>

```bash
pip install -r requirements.txt
python -m playwright install --with-deps chromium   # 网页登录、自动注册需要
cp .env.example .env && vim .env
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

</details>

<details>
<summary><b>部署到公网服务器</b></summary>

**务必先加鉴权再暴露端口。** 服务本身不做 HTTPS，请用反代终结 TLS。

```bash
# 1. 只绑本机，由反代对外
#    docker-compose.yml:  ports: ["127.0.0.1:8000:8000"]

# 2. 设置强密钥
#    .env:  API_KEY=$(openssl rand -hex 24)
#           ADMIN_KEY=$(openssl rand -hex 24)

# 3. 反代加基础认证（示例：Caddy）
#    your.domain {
#        basicauth { admin <bcrypt-hash> }
#        reverse_proxy 127.0.0.1:8000
#    }
```

暴露前的自检：

```bash
curl -i http://<你的地址>/v1/models                    # 期望 401，返回 200 = 密钥没生效，停下排查
curl -i -H "Authorization: Bearer $API_KEY" http://<你的地址>/healthz
```

</details>

---

## 加账号的三种方式

在管理端 `/admin` 点对应按钮即可。

| 方式 | 适用 | 要做的事 |
|---|---|---|
| **① 网页登录**（推荐） | 所有情况 | 服务器拉起真实浏览器，画面实时推到你屏幕，你直接操作，登录成功自动入库 |
| **② 自动注册** | 有邮箱域名池 | 全自动：开浏览器 → 填邮箱 → 过滑块 → 收验证码 → 入库 |
| **③ 粘贴 Cookie** | 想最快 | 从浏览器 F12 复制 `_m_h5_tk` / `cookie2` / `xman_t` 等，粘贴即可 |

方式 ①② 需要配置取码后端（`cloudmail` / `imap` / `manual`），见 **[docs/EMAIL.md](docs/EMAIL.md)**。

---

## 批量注册

配置邮箱域名池后，一次派生多个别名、各注册一个账号：

```bash
curl -X POST http://localhost:8000/admin/api/register/batch \
  -H "X-Admin-Key: $ADMIN_KEY" -H "Content-Type: application/json" \
  -d '{"primary_email":"you@yourdomain.com","count":5}'
```

原理与配置见 **[docs/EMAIL.md](docs/EMAIL.md)**。

---

## 接入客户端

| 客户端 | 填法 |
|---|---|
| Cherry Studio / ChatBox | 服务地址 `http://<host>:8000/v1`，密钥 = 你的 `API_KEY` |
| New API / One API | 渠道类型选 **OpenAI**，代理地址 `http://<host>:8000` |
| curl | 见上方示例 |

模型名填 Accio 那边的实际模型（如 `deepseek-v4.1`）。

---

## 功能一览

| 能力 | 说明 |
|---|---|
| **协议翻译** | Accio WebSocket 流式 ↔ 标准 SSE，`n>1`、`include_usage`、工具调用均已适配 |
| **凭证池** | 多账号加密存储（AEAD）、健康巡检、失效自动隔离 |
| **代理池 / IP 轮换** | 单账号也可走代理出口，见 [docs/proxy.md](docs/proxy.md) |
| **邮箱别名引擎** | 派生 `you+tag@…` 或 `tag@yourdomain`，区分「发给谁」的验证码 |
| **调用审计** | 每次调用落盘：模型 / 用量 / 耗时 / 结果，Web UI 可查 |
| **系统提示词控制** | 压制上游人设、拦截身份标识泄露，可开关 |
| **管理界面** | 双端适配，手机浏览器可操作 |

---

## 安全与限额

- **限流**：三条独立线（推理按 API key / 管理按 IP / 登录更严），`RATE_LIMIT_ENABLED=false` 可关
- **凭证加密**：AEAD + 拒绝明文降级 + 0600 落盘；解密失败会记 error 并在巡检中暴露
- **信任边界**：`X-Forwarded-For` 仅在 `TRUSTED_PROXIES` 配置时采信
- **别名归属**：主地址兜底**默认关闭**，避免同收件箱下多账号互相认领验证码

完整清单见 **[docs/SECURITY.md](docs/SECURITY.md)**。

---

## 配置

推荐用向导（`python setup.py`）生成，它会只问必选项。要手动改，看 `.env.example`
——每项都有注释。改动后重启服务生效。

常改的几项：

| 变量 | 说明 |
|---|---|
| `API_KEY` | 客户端访问 `/v1/*` 的密钥。**不设则接口不校验密钥**，生产必须设 |
| `ADMIN_KEY` | 登录 `/admin` 的密钥。不设则首次启动自动生成到 `data/.admin_key` |
| `SECRET_KEY` | 凭证加密主密钥。**务必单独备份**，丢了已存账号全部不可恢复 |
| `OTP_BACKEND` | `cloudmail` / `imap` / `manual`，见 [docs/EMAIL.md](docs/EMAIL.md) |
| `ALLOWED_HOSTS` | Host 白名单，公网部署时设为你的域名 |
| `RATE_LIMIT_ENABLED` | 限流开关，默认 `true` |

> 已存在的环境变量优先级高于 `.env` —— `API_KEY=xxx docker compose up` 可临时压过配置文件。

---

## 文档

| 文件 | 内容 |
|---|---|
| `python setup.py` | 交互式配置向导（本文「30 秒跑起来」） |
| [docs/EMAIL.md](docs/EMAIL.md) | 取码后端（cloudmail / imap / manual）与别名引擎详解 |
| [docs/SECURITY.md](docs/SECURITY.md) | 安全模型、限流、审计、已知边界 |
| [docs/proxy.md](docs/proxy.md) | 代理池与 IP 轮换 |
| [.env.example](.env.example) | 全部配置项与注释 |

---

## 许可证

[MIT](LICENSE)

> 本项目仅供个人学习与技术研究。使用者需自行确保拥有所使用账号的合法处置权、
> 遵守 Accio 服务条款与所在地区法律法规。作者不对滥用行为负责。
