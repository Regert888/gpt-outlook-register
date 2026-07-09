# WebUI systemd 服务部署手册

把 `start_webui.py` 注册成 systemd 服务后，可实现开机自启、崩溃自动重启、统一日志管理。

---

## 1. 编写服务配置文件

在 `/etc/systemd/system/` 下创建服务单元文件，例如 `gpt-outlook-webui.service`：

```bash
vim /etc/systemd/system/gpt-outlook-webui.service
```

### 1.1 完整示例

```ini
[Unit]
Description=GPT Outlook Register WebUI
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
Group=root
WorkingDirectory=/root/codes/gpt-outlook-register
Environment=AUTH_HTTP_TRACE=1
Environment=PATH=/root/.nvm/versions/node/v24.18.0/bin:/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/games:/usr/local/games:/snap/bin
ExecStart=/root/.local/bin/uv run start_webui.py \
    --host 0.0.0.0 \
    --port 8765 \
    --ssl-keyfile /etc/letsencrypt/live/example.com/privkey.pem \
    --ssl-certfile /etc/letsencrypt/live/example.com/fullchain.pem \
    --token 你的实际Token
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=gpt-outlook-webui

[Install]
WantedBy=multi-user.target
```

### 1.2 关键字段说明

| 字段 | 说明 |
|------|------|
| `Description` | 服务的可读名称，显示在 `systemctl status` 中。 |
| `After` / `Wants` | 等网络就绪后再启动，避免绑定端口时网络未初始化。 |
| `Type=simple` | 前台运行的服务，systemd 会跟踪主进程。 |
| `User` / `Group` | 以哪个用户身份运行。示例使用 `root`，因为 `/etc/letsencrypt` 证书默认仅 root 可读；如使用普通用户，请确保证书文件或目录对该用户可读。 |
| `WorkingDirectory` | 项目根目录，`uv run` 需要在这里找到 `pyproject.toml` 或依赖环境。 |
| `Environment` | 设置环境变量，可写多行。例如 `AUTH_HTTP_TRACE=1`。 |
| `ExecStart` | 实际启动命令，建议用 `uv` 的绝对路径。 |
| `Restart=on-failure` | 进程异常退出时自动重启。 |
| `RestartSec=5` | 每次重启间隔 5 秒，防止刷屏式重启。 |
| `StandardOutput/Error=journal` | 把标准输出和错误输出交给 systemd journal。 |
| `WantedBy=multi-user.target` | 开机时随系统进入多用户模式启动。 |

### 1.3 常用自定义

**不启用 HTTPS（内网或前面有反向代理）：**

```ini
ExecStart=/root/.local/bin/uv run start_webui.py \
    --host 0.0.0.0 \
    --port 8765 \
    --token 你的实际Token
```

**从文件读取 Token（更安全，避免明文写在服务文件里）：**

```bash
# 1. 创建 token 文件并设置权限
mkdir -p /etc/gpt-outlook-webui
echo "你的实际Token" | tee /etc/gpt-outlook-webui/token.txt > /dev/null
chmod 600 /etc/gpt-outlook-webui/token.txt
chown root:root /etc/gpt-outlook-webui/token.txt
```

```ini
ExecStart=/root/.local/bin/uv run start_webui.py \
    --host 0.0.0.0 \
    --port 8765 \
    --token-file /etc/gpt-outlook-webui/token.txt
```

**多环境变量：**

```ini
Environment="AUTH_HTTP_TRACE=1"
Environment="OPENAI_API_KEY=sk-xxx"
EnvironmentFile=-/etc/gpt-outlook-webui/env
```

`EnvironmentFile` 指向一个 `.env` 文件，每行一个 `KEY=VALUE`，适合放敏感配置。

---

## 2. 加载配置并设置开机自启

```bash
# 重新加载 systemd 配置
systemctl daemon-reload

# 设置开机自启
systemctl enable gpt-outlook-webui.service
```

---

## 3. 启动、停止、重启、查看状态

### 3.1 启动服务

```bash
systemctl start gpt-outlook-webui.service
```

### 3.2 停止服务

```bash
systemctl stop gpt-outlook-webui.service
```

### 3.3 重启服务

```bash
systemctl restart gpt-outlook-webui.service
```

### 3.4 查看运行状态

```bash
systemctl status gpt-outlook-webui.service
```

输出中会显示：
- `Active: active (running)` / `inactive (dead)` / `failed`
- 进程 PID
- 最近几十行日志

### 3.5 取消开机自启

```bash
systemctl disable gpt-outlook-webui.service
```

---

## 4. 查看日志

### 4.1 实时跟踪日志

```bash
journalctl -u gpt-outlook-webui.service -f
```

按 `Ctrl+C` 退出。

### 4.2 查看最近 100 行日志

```bash
journalctl -u gpt-outlook-webui.service -n 100
```

### 4.3 查看今天全部日志

```bash
journalctl -u gpt-outlook-webui.service --since today
```

### 4.4 按时间范围查看

```bash
journalctl -u gpt-outlook-webui.service --since "2026-07-06 10:00:00" --until "2026-07-06 12:00:00"
```

### 4.5 只看错误级别日志

```bash
journalctl -u gpt-outlook-webui.service -p err
```

### 4.6 清空旧日志（谨慎）

```bash
journalctl --vacuum-time=7d
```

---

## 5. 常见问题

### 5.1 服务启动失败，状态显示 `failed`

```bash
systemctl status gpt-outlook-webui.service
journalctl -u gpt-outlook-webui.service -n 50
```

常见原因：
- `uv` 路径不对：用 `which uv` 确认绝对路径。
- 工作目录不对：`WorkingDirectory` 必须指向项目根目录。
- SSL 证书文件不存在或无权限：检查 `/etc/letsencrypt/...` 是否存在，以及 `User` 是否有读取权限。
- Token 未填写： `--token xxx` 会当成字面量 `xxx`。

### 5.2 修改配置后生效

每次修改 `/etc/systemd/system/xxx.service` 后都要执行：

```bash
systemctl daemon-reload
systemctl restart gpt-outlook-webui.service
```

### 5.3 如何彻底删除服务

```bash
SERVICE=gpt-outlook-webui
systemctl stop ${SERVICE}.service
systemctl disable ${SERVICE}.service
rm -f /etc/systemd/system/${SERVICE}.service
rm -rf /etc/systemd/system/${SERVICE}.service.d/
systemctl daemon-reload
systemctl reset-failed ${SERVICE}.service
```

---

## 6. 快速检查清单

- [ ] `User` 和 `Group` 是实际存在的用户/组
- [ ] `WorkingDirectory` 指向项目根目录
- [ ] `ExecStart` 中的 `uv` 是绝对路径
- [ ] 如果启用 HTTPS，证书路径存在且运行用户可读
- [ ] Token 已替换为真实值，或使用 `--token-file`
- [ ] 执行了 `daemon-reload` 和 `enable`
- [ ] 防火墙/安全组放行对应端口（如 8765）
