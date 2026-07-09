# ChatGPT 注册（纯协议 + Outlook 邮箱版）—— 最小化提取包

技术交流群：259844673


<img width="280" height="265" alt="image" src="https://github.com/user-attachments/assets/a24f9f9b-0dfa-440e-8ee8-6e461d03eeea" />


**完全无浏览器**：用 `curl_cffi` 模拟 TLS 指纹 + 纯 Python/QuickJS 解 OpenAI Sentinel PoW + IMAP XOAUTH2 取 OTP，直接走 OpenAI authorize 状态机。

含轻量级 **WebUI**：批量导入号池、可视化触发注册、实时 SSE 日志、凭证一键复制。
**邮箱来源支持双模式**：Outlook 接码池 + CF Worker 自建 catch-all（`cloudflare_temp_email`）。
不含支付、daemon、Camoufox、Playwright。

## 📰 最近更新（2026-06）

- 📱 **SMS 接码集成（解决 add-phone 拿不到 refresh_token 的问题）**
  - 「📱 接码配置」Tab 一键启用SmsBower平台
  - 命中 `add-phone` 时自动租号 → 接 SMS → 验证，**全程无人值守**
  - 支持 **号码复用**（SmsBower：一个号注册多个 ChatGPT，省钱）
  - 支持 **自动选最优国家**（按价格 + 库存）
  - WebUI 提供「测试余额」「查询国家排名」按钮
  - 兼容旧的 `OPENAI_PHONE_NUMBER` / `OPENAI_PHONE_OTP` 环境变量路径（未启用接码时自动回退）
- ✨ **CF Temp Email 自建邮箱支持**
  - 「📧 邮箱配置」Tab 切换邮箱来源（outlook / cf_temp）
  - 支持 `dreamhunter2333/cloudflare_temp_email` 部署的 Worker
  - CF 模式下不需要 outlook 接码号，注册时随机生成 catch-all 邮箱
  - 内置 `/admin/new_address` + `/admin/mails` 接口对接 + Bot Fight Mode 绕过
- 🔄 **账号重置功能**：done/failed 状态的号一键重置回 available；支持批量勾选重置
- 🚀 **多 worker 并发**（1-20）+ 代理池 round-robin：auto-loop 改成多线程，每个 worker 独立代理
- 🩺 **错误分类 + 熔断**：网络错误自动 release 号；连续 3 次网络错误自动暂停 + 红色横幅
- 📦 **批量操作**：批量删除号池/凭证 + 按状态批量删
- ✅ **session_token 三路兜底**：cookie / JSON / domain-free，新号也能拿到

## 🎀 两种使用方式

### A. WebUI 模式（推荐，可视化）

#### 全新安装
```bash
git clone https://github.com/Regert888/gpt-outlook-register.git
cd gpt-outlook-register
pip install -r requirements.txt
python start_webui.py
# 浏览器自动打开 http://127.0.0.1:8765/
```

> 默认端口 8765，可用 `--port` 指定其他端口。

**公网启动**：
```bash
python start_webui.py --host 0.0.0.0 --port 8765
```

#### Linux 服务器后台运行（24 小时不停）

**推荐使用 screen 后台运行**，关闭 SSH 后进程不会被杀掉：

```bash
# 1. 安装 screen（如果没有）
apt install -y screen   # Debian/Ubuntu
# yum install -y screen  # CentOS/RHEL

# 2. 创建 screen 会话
screen -S webui

# 3. 启动 WebUI（公网访问）
python start_webui.py --host 0.0.0.0 --port 8765

# 4. 看到启动成功后，按 Ctrl+A 再按 D 离开
#    （进程继续在后台运行）

# 5. 浏览器访问
#    http://服务器公网IP:8765

# --- 常用命令 ---
# 重新连接到 screen（查看日志）
screen -r webui

# 查看所有 screen 会话
screen -ls

# 停止 WebUI（在 screen 里按 Ctrl+C）
```

**重要提示**：
- 需要先安装 Node.js（≥18）才能正常收到 OTP 邮件，否则会卡在验证码等待：
  ```bash
  curl -fsSL https://deb.nodesource.com/setup_20.x | sudo bash -
  sudo apt-get install -y nodejs
  node --version  # 验证 >= 18
  ```

#### 已有项目，安全升级（不丢数据）
```bash
cd gpt-outlook-register

# 1. 🔴 备份数据库（重要！）
cp webui/webui.db webui/webui.db.backup
# Windows 用户也可以手动复制 webui\webui.db 到别的地方

# 2. 拉取最新代码
git pull

# 3. 重启 webui
python start_webui.py
```

**说明**：
- `webui.db` 已在 `.gitignore`，`git pull` **不会覆盖或删除**它
- 新版本会自动执行 DB migration（添加新表），**不会删除**已有数据
- 如果担心出问题，先 `cp webui.db webui.db.backup` 备份

---

WebUI 提供：
- 📥 批量粘贴 4 段格式 → 一键入库
- 📋 号池列表 + 状态过滤（available/in_use/done/failed）
- ✅ 凭证勾选：`session_token` / `refresh_token` 用户可选
- ▶️ 一键触发注册，实时 SSE 日志（彩色分级）
- 📤 注册结果表 + 凭证 JSON 复制按钮

### B. 命令行模式（单号注册）
```bash
python register_outlook.py 'email----password----client_id----refresh_token'
```

## 文件清单

### 核心库（命令行 + WebUI 共用）
| 文件 | 行数 | 作用 |
|---|---|---|
| `register_outlook.py` | ~100 | **命令行入口**：4 段格式 → `AuthFlow.run_register` → 写出账号 JSON |
| `auth_flow.py` | ~2790 | **纯协议核心**：csrf → authorize → sentinel → signup → otp → create_account → callback → token exchange |
| `mail_outlook.py` | ~300 | Outlook IMAP XOAUTH2 取 OTP（refresh_token 续期 + 多 folder + tm1 影子过滤） |
| `mail_cf.py` | ~260 | CF Worker 自建邮箱 Provider（`cloudflare_temp_email` 兼容；OTP 抽取防误判：HTML span 优先 + 排除邮箱/时间戳/hex 颜色） |
| `sentinel.py` | ~290 | OpenAI Sentinel Token 纯 Python PoW（FNV-1a 32-bit） |
| `sentinel_quickjs.py` | ~270 | Sentinel Token QuickJS 路径（跑 OpenAI sdk.js，需要 node） |
| `openai_sentinel_quickjs.js` | ~400 | QuickJS 路径用的 sdk.js wrapper |
| `sms_provider.py` | ~700 | **SMS 接码 provider**：BaseSmsProvider + SmsActivate / HeroSMS / SmsBower + PhoneCallbackController |
| `http_client.py` | ~70 | `curl_cffi` Chrome136 TLS 指纹包装 |
| `config.py` | ~15 | 极简 Config（只留 `proxy` 字段） |

### WebUI（可选）
| 文件 | 作用 |
|---|---|
| `start_webui.py` | 一键启动脚本（自动装依赖 + 打开浏览器） |
| `webui/app.py` | FastAPI 主程序（路由 + SSE 流式日志） |
| `webui/db.py` | SQLite 号池 + 注册结果存储 |
| `webui/registrar.py` | 注册任务 worker（独立线程 + 日志回调） |
| `webui/static/index.html` | 单页 SPA |
| `webui/static/style.css` | 粉色清爽样式 ~ |
| `webui/static/app.js` | 前端交互 + SSE 接收 |
| `requirements.txt` | Python 依赖（含 fastapi/uvicorn） |

## 完整协议链路

```
register_outlook.py
    └── AuthFlow(cfg).run_register(mail_provider)
            ├── [1/10] GET  chatgpt.com/api/auth/csrf                       → csrf_token
            ├── [2/10] POST chatgpt.com/api/auth/signin/openai              → auth_url (含 client_id)
            ├── [3/10] GET  auth.openai.com/authorize?...                   → device_id (oai-did cookie)
            ├── [4/10] POST sentinel.openai.com/backend-api/sentinel/req    → sentinel_token (PoW)
            ├── [5/10] POST auth.openai.com/api/accounts/authorize/continue → 判定 signup vs 已有账号
            │           (screen_hint=signup, openai-sentinel-token=...)
            ├── [5.5]  POST auth.openai.com/api/accounts/user/register      → 注册密码
            ├── [6/10] GET  auth.openai.com/api/accounts/email-otp/send     → 触发 OTP 邮件
            │           或 POST .../passwordless/send-otp
            │           或 POST .../email-otp/resend
            ├── mail_provider.wait_for_otp() ←──── IMAP XOAUTH2 拉 outlook OTP
            ├── [7/10] POST auth.openai.com/api/accounts/email-otp/validate → 验证 OTP
            ├── [8/10] POST auth.openai.com/api/accounts/create_account     → 填 name/birthdate
            ├── [9/10] follow_redirect_chain()                              → 抓 callback URL (含 code)
            ├── [10/10] GET chatgpt.com/api/auth/session                    → access_token + cookies
            └── [可选] POST auth.openai.com/oauth/token (Codex PKCE)        → refresh_token
```

## Outlook 4 段接码格式

```
email----password----client_id----microsoft_refresh_token
```

例：
```
charles@outlook.jp----<pwd>----9e5f94bc-e8a4-...----M.C538_BAY.0.U.-Cs...
```

> `client_id` 必须具备 v2 endpoint 的 `https://outlook.office.com/IMAP.AccessAsUser.All offline_access` scope。
> 实测 supplier 卖的接码号若用 Thunderbird client_id Device Code Flow 重拿的 refresh_token 才有 IMAP scope。

## 安装

```bash
pip install -r requirements.txt
```

**可选**：装 Node.js（≥18）启用 QuickJS sentinel 路径——纯 Python 路径只能过 sentinel `/req` 表层校验，
OpenAI 服务端跑真 sdk.js 深层校验时会判定为非浏览器 → OTP 邮件 silent-drop（200 OK 但不下发）。
要真稳定收到 OTP 邮件，**强烈建议装 node**。

```bash
# Ubuntu/Debian
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo bash -
sudo apt-get install -y nodejs

# 验证
node --version  # 需要 >= 18
```

## 使用

### 1. 单跑 OTP 取码（确认 outlook 接码号能用）
```bash
python mail_outlook.py 'email----password----client_id----refresh_token'
# → OTP: 123456  (需要 OpenAI 已发过码进该邮箱)
```

### 2. 完整纯协议注册
```bash
python register_outlook.py 'email----password----client_id----refresh_token'
```

成功输出 `account_<email>.json`：
```json
{
  "email": "...",
  "password": "...",
  "session_token": "...",   // __Secure-next-auth.session-token cookie
  "access_token": "...",    // /api/auth/session 返回
  "device_id": "...",       // oai-did cookie
  "csrf_token": "...",
  "id_token": "...",
  "refresh_token": "...",   // Codex OAuth 拿的 RT
  "cookie_header": "..."    // chatgpt.com 全 cookie 拼接
}
```

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `PROXY` | - | 出口代理 URL，例 `socks5://user:pass@host:port`（curl_cffi 会自动 socks5→socks5h） |
| `OTP_TIMEOUT` | `60` | OTP 等待秒数（实际下限 30） |
| `WEBUI_ALLOW_LOGIN` | `0` | `1`=邮箱被识为已注册时走 OTP login；`0`=fast-fail 抛错 |
| `SKIP_OAUTH_TOKEN_EXCHANGE` | `0` | `1`=跳过 OAuth refresh_token 交换 |
| `OAUTH_CODEX_RT_EXCHANGE` | `1` | `1`=尝试 Codex OAuth 拿 refresh_token |
| `OAUTH_REFRESH_ONLY` | `0` | `1`=只要 refresh_token，跳过 session 步骤 |
| `OPENAI_SENTINEL_DISABLE_QUICKJS` | - | 设任意值 = 禁用 QuickJS，仅用纯 Python PoW |
| `OPENAI_SENTINEL_NODE_PATH` | `node` | node 二进制路径 |
| `LOGIN_PASSWORD` | - | 已有账号 login_password 分支用的密码（不设则取 email 去 @） |
| `AUTH_HTTP_TRACE` | `0` | `1`=打印每次 HTTP 请求 method/url/status/cookies（调试用） |
| `AUTH_TRACE_DUMP` | `0` | `1`=写完整 HTTP request/response 到 `outputs/auth_trace_*.jsonl` |
| `OPENAI_PHONE_NUMBER` | - | 手动 add-phone 时的号码（用 `,` 分隔多个候选）。**未启用 SMS 接码时用** |
| `OPENAI_PHONE_OTP` | - | 静态 OTP 验证码（调试用，6 位数字） |
| `OPENAI_PHONE_OTP_CMD` | - | 动态获取手机 OTP 的 shell 命令（stdout 含 6 位数字即命中） |
| `OPENAI_PHONE_OTP_TIMEOUT` | `180` | add-phone 等 SMS 超时（秒） |

## 📱 SMS 接码集成

OpenAI 注册有概率被风控跳到 `add-phone` 步骤，**不绑手机号就拿不到 refresh_token**。
WebUI「📱 接码配置」Tab 启用接码后，命中 add-phone 时会自动租号 + 等 SMS + 自动验证。

### 支持的平台

| 平台 | 网址 | 特色 |
|---|---|---|
| **HeroSMS** | hero-sms.com | 支持号码复用（同号注册多个 ChatGPT 省钱）+ V2 API + auto-resend，**推荐** |
| **SmsBower** | smsbower.page | API 兼容 HeroSMS |
| **SMS-Activate** | sms-activate.guru | 国际老牌，纯文本协议 |

### ⚠️ 关键事实（截至 2026 实测）

OpenAI 自 2025 年起对大部分国家改用 **WhatsApp** 验证，
纯 SMS 路径**目前只有泰国（country_id=52）确认稳定可用**，其它国家可能抽到 WhatsApp 号导致拿不到 SMS。

WebUI 内置了「OpenAI 走 SMS 国家白名单」，启用自动选国家时只会从白名单里挑。
查询国家排名按钮也会高亮显示哪些国家是 SMS 可用的。

### 使用步骤

1. 注册平台账号 + 充值 + 拿 API Key
2. WebUI →「📱 接码配置」→ 启用 + 填 API Key + 选国家（默认 52 = Thailand）→ 保存
3. 点「测试余额」确认连通
4. 正常触发注册，命中 add-phone 时会自动租号，日志里能看到 `📱 准备租号` / `✅ 已租到号码` / `⏳ 等待 SMS 验证码` / `🎉 已标记号码成功完成`

### 配置项说明

| 配置项 | 说明 |
|---|---|
| `sms_enabled` | 总开关；关闭时回退到 `OPENAI_PHONE_NUMBER` 环境变量路径 |
| `sms_provider` | herosms / smsbower / sms_activate |
| `sms_country` | 国家代码或 ID，默认 `52`（Thailand） |
| `sms_service` | OpenAI = `dr` |
| `sms_max_price` | 单号最高价格（HeroSMS / SmsBower 用，平台货币单位） |
| `sms_reuse_phone` | 同号复用（HeroSMS / SmsBower） |
| `sms_phone_success_max` | 同号最多复用 N 次 |
| `sms_auto_country` | 自动选最优国家（限白名单） |

## 已知坑（来自原项目实测）

### Sentinel Token 深层校验
- **纯 Python sentinel**：`/sentinel/req` 返 200 OK，`/authorize/continue` 也返 200 OK，
  但 OpenAI 服务端做 OTP 派发前会跑**真 sdk.js**校验 token，纯 Python PoW 会被判为非浏览器 → 邮件 silent-drop。
- **QuickJS sentinel（默认启用）**：跑 OpenAI 真 `sdk.js`（自动下载缓存到 `/tmp/openai-sentinel-demo/<ver>/sdk.js`），返回真实浏览器同款 token，OTP 派发正常。
- 没装 node 也能跑，但只能用于纯协议探测，**不能真发码**。

### Outlook 收件细节
- **Junk 邮件**：OpenAI 首次发码给陌生收件人常被 outlook 反垃圾分到 Junk，本脚本扫多 folder（INBOX / Junk / Junk Email / Spam）。
- **tm1.openai.com 影子发码**：OpenAI 当前坏掉的发码域，所有账号都返固定 OTP=493682 → verify 401。已硬过滤。
- **refresh_token 自动滚动续期**：Microsoft access_token 1 天有效，本模块每 3000s 自动续期。

### OpenAI 反欺诈
- **已有账号分支**：邮箱被识为已注册时，默认 fast-fail 抛 `RuntimeError`（防 honeypot）。
  设 `WEBUI_ALLOW_LOGIN=1` 改走 OTP login 拿凭证。
- **`authorize/continue` 已 trigger 发码**：passwordless 模式下 OpenAI 已经发了码，
  此时 **必须只 resend**，不能再调 `email-otp/send`（会新建 challenge → 旧 OTP 失效 → wrong_email_otp_code）。
  脚本里 `kickoff_otp_delivery` 已根据 `_is_existing_account` 自动切分支。
- **批量注册次日存活率约 2%**（原项目反欺诈研究数据），单号 / 偶发使用没问题。

### 网络层
- **curl_cffi SOCKS5**：默认会把 `socks5://` 规范化成 `socks5h://`（DNS 走代理解析），减少 TLS 握手异常。
- **TLS 指纹**：默认 `chrome136`；TLS 握手失败时会自动尝试 `chrome124` / `chrome120` 兜底。
- **Cloudflare 403**：`get_csrf_token` 内置 3 次重试 + 指数 backoff。

### SMS 接码
- **WhatsApp vs SMS**：OpenAI 自 2025 年起对大部分国家改用 WhatsApp 验证。如果租到的国家不在白名单（目前只有泰国 52），接码平台收不到 SMS → 超时 → cancel 退款。
- **HeroSMS 物理号识别**：动态查实际价格作为 `maxPrice`（实际价 × 3 作上限），避免被分配虚拟号（虚拟号收不到 OpenAI 的 SMS）。
- **号码复用**：HeroSMS / SmsBower 在 20 分钟生命周期内同号最多复用 N 次（默认 3）。一个号注册多个 ChatGPT 可显著降本，但前提是 OpenAI 端没风控同号。
- **OTP validate 失败 → resend 兜底**：phone-otp/validate 401 时会自动 resend 一次再等新码（OpenAI 偶尔拒第一条码但接受第二条）。


