# Nginx 反向代理 + Basic Auth 部署手册

使用 `nginxinc/nginx-unprivileged:alpine` 镜像在前端做反向代理 + Basic Auth，把 WebUI 放到 Nginx 后面，对外只暴露 Nginx 端口，避免 WebUI 直接裸奔公网。

> 镜像名：`nginxinc/nginx-unprivileged:alpine`（非 root 运行，容器内直接监听 47279（≥1024，非 root 可绑），配置/证书目录都和官方 `nginx` 镜像不同，详见下文）。

实际部署参数：

| 项 | 值 |
|----|----|
| 域名 | `your-domain.example.com` |
| Nginx 端口 | `47279`（HTTPS，容器内直接监听，对外即此端口） |
| WebUI 内部端口 | `8765` |
| 回源方式 | `host.docker.internal`（docker bridge 网络） |
| TLS 证书 | 拷贝自 `/etc/letsencrypt/live/your-domain.example.com/`，副本放在 `/etc/gpt-outlook-nginx/certs/` |

---

## 0. 拓扑

```
客户端 ──HTTPS──▶ Nginx容器(47279 ssl) ──Basic Auth──▶ http://host.docker.internal:8765
                                                          │
                                                 start_webui.py (host 0.0.0.0:8765)
```

- WebUI 监听 `0.0.0.0:8765`，但宿主防火墙**只放行 docker 网段**，公网进不来，由 Nginx 回源。
- TLS 由 Nginx 终结，WebUI 不再需要 `--ssl-*`。
- 认证只走 Nginx Basic Auth 一层；WebUI 的 `--token` 必须关掉（避免与 Basic Auth 互相打架导致密码框无限弹，见 §10.8）。

---

## 1. 改造 WebUI systemd 服务

当前 `start_webui.py` 监听 `127.0.0.1:8765` 且自带 SSL。改用 Nginx 反代 + bridge 回源后，需调整为：

- `--host 0.0.0.0`：让 docker 网段的 Nginx 容器能访问到（仍由防火墙限制公网）。
- 去掉 `--ssl-keyfile / --ssl-certfile`：TLS 上交给 Nginx 终结，避免 double TLS。
- **去掉 `--token`**：WebUI 浏览器访问走 Nginx Basic Auth 即可，同时开 `--token` 会导致密码框无限弹（见 §10.8）。

修改后的 `/etc/systemd/system/gpt-outlook-webui.service`：

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
    --port 8765
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=gpt-outlook-webui

[Install]
WantedBy=multi-user.target
```

变更点（相对原配置）：

| 字段 | 原来 | 现在 | 原因 |
|------|------|------|------|
| `--host` | `127.0.0.1` | `0.0.0.0` | bridge 网络的 Nginx 容器需经 `host.docker.internal` 回源，`127.0.0.1` 进不来 |
| `--ssl-keyfile` / `--ssl-certfile` | 有 | 删除 | TLS 终结上移到 Nginx |
| `--token` | 有 | 删除 | 与 Nginx Basic Auth 冲突导致浏览器无限弹框（见 §10.8） |

> **注意行尾续行符**：`--port 8765` 是 `ExecStart` 的**最后一个参数**，这行**末尾不能有 `\`**，否则 systemd 会把下面的 `Restart=on-failure` 当成参数传给 `start_webui.py`，启动直接报 `unrecognized arguments: Restart=on-failure`。删参数时记得同步删对应行尾的 `\`。

重载服务：

```bash
systemctl daemon-reload
systemctl restart gpt-outlook-webui.service
systemctl status gpt-outlook-webui.service
```

> 临时回源调试可直接 `curl 127.0.0.1:8765/` 验证 WebUI 已起来。

---

## 2. 锁住 8765（关键安全步骤）

WebUI 改 `0.0.0.0` 后理论被公网可达，必须用防火墙只放行 docker 网段、挡掉外部直连 8765。

以 ufw 为例：

```bash
# 放行对外 Nginx 端口
ufw allow 47279/tcp

# 放行 docker bridge 网段到 8765
ufw allow in on docker0 to any port 8765 proto tcp

# 如用自定义 compose 网段（默认 172.16.0.0/12 / 192.168.0.0/16），也可显式写：
# ufw allow from 172.16.0.0/12 to any port 8765 proto tcp

ufw reload
ufw status verbose
```

验证从公网进不来：

```bash
# 在另一台机器上
curl -i --max-time 5 https://your-domain.example.com:8765/   # 应 timeout / refused
```

---

## 3. 准备目录与文件

```bash
sudo mkdir -p /etc/gpt-outlook-nginx/conf /etc/gpt-outlook-nginx/auth /etc/gpt-outlook-nginx/certs
```

目录结构：

```
/etc/gpt-outlook-nginx/
├── conf/
│   └── default.conf        # nginx server 配置
├── auth/
│   └── .htpasswd           # Basic Auth 用户密码
└── certs/                  # 从 Let's Encrypt 拷贝过来的副本
    ├── fullchain.pem
    └── privkey.pem
```

> 注意：用的是 **拷贝副本**而不是软链。原因：① 不动 Let's Encrypt 默认权限，不影响同机其它使用证书的服务；② UID 101 容器只需对 `/etc/gpt-outlook-nginx/certs` 有权限，不用放开 `/etc/letsencrypt/*`；③ 续期后用 hook 拷贝+reload，路径稳定。

### 3.1 拷贝证书副本并设权限

```bash
# 拷一份过来（保留 cp 跟随软链读真实文件的能力）
sudo cp -L /etc/letsencrypt/live/your-domain.example.com/fullchain.pem /etc/gpt-outlook-nginx/certs/fullchain.pem
sudo cp -L /etc/letsencrypt/live/your-domain.example.com/privkey.pem  /etc/gpt-outlook-nginx/certs/privkey.pem

# 让 UID 101 worker 能读
sudo chown -R 101:101 /etc/gpt-outlook-nginx/certs
sudo chmod 644 /etc/gpt-outlook-nginx/certs/fullchain.pem
sudo chmod 640 /etc/gpt-outlook-nginx/certs/privkey.pem
```

> Let's Encrypt 目录权限无需任何改动。

---

## 4. 生成 Basic Auth 密码文件

### 4.1 用 htpasswd 生成（推荐 bcrypt）

```bash
# 单用户（覆盖式）
docker run --rm httpd:alpine htpasswd -nbB 你的用户名 '你的密码' \
  | sudo tee /etc/gpt-outlook-nginx/auth/.htpasswd
```

多用户追加（**不带 `-c`**，`-c` 会覆盖文件）：

```bash
docker run --rm httpd:alpine htpasswd -nbB 第二个用户 '第二个密码' \
  | sudo tee -a /etc/gpt-outlook-nginx/auth/.htpasswd
```

### 4.2 修正权限

`nginx-unprivileged` 容器内 worker 以 UID 101 运行，必须能读 `.htpasswd`：

```bash
sudo chown -R 101:101 /etc/gpt-outlook-nginx/auth
sudo chmod 640 /etc/gpt-outlook-nginx/auth/.htpasswd
```

---

## 5. 编写 Nginx 配置

`/etc/gpt-outlook-nginx/conf/default.conf`：

```nginx
server {
    listen 47279 ssl;
    http2 on;
    server_name your-domain.example.com;

    ssl_certificate     /etc/nginx/certs/fullchain.pem;
    ssl_certificate_key /etc/nginx/certs/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_session_cache   shared:SSL:10m;
    ssl_session_timeout 10m;

    location / {
        auth_basic           "Restricted";
        auth_basic_user_file /etc/nginx/auth/.htpasswd;

        proxy_pass http://host.docker.internal:8765;

        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # WebUI 用 SSE 推日志，必须关 buffer + 拉长超时
        proxy_buffering    off;
        proxy_cache        off;
        proxy_read_timeout 24h;
        proxy_send_timeout 24h;
    }
}
```

要点：

- `listen 47279 ssl`：47279 ≥ 1024，非 root 容器可直接绑，对外即此端口，无需端口映射改写。
- `proxy_pass http://host.docker.internal:8765`：通过 `--add-host=host.docker.internal:host-gateway` 解到宿主，所以 WebUI 必须 `--host 0.0.0.0`。
- `proxy_buffering off` + 24h 超时：WebUI 的 SSE 日志流不能被缓存截断。
- 想同时启用 Basic Auth + WebUI 的 `--token` 做双层保护，直接保留两边都开即可。

---

## 6. 用 docker-compose 启动

`/etc/gpt-outlook-nginx/docker-compose.yml`：

```yaml
services:
  nginx:
    image: nginxinc/nginx-unprivileged:alpine
    container_name: gpt-outlook-nginx
    restart: unless-stopped
    extra_hosts:
      # 让容器内 host.docker.internal 解析到宿主网关
      - "host.docker.internal:host-gateway"
    ports:
      - "47279:47279"     # 容器内直接 listen 47279 ssl，对外即此端口
    volumes:
      - ./conf:/etc/nginx/conf.d:ro     # 用 ./ 表示相对 compose 文件目录
      - ./auth:/etc/nginx/auth:ro
      - ./certs:/etc/nginx/certs:ro
    # 可选：实时日志级别
    # command: ["nginx", "-g", "daemon off; error_log /dev/stderr info;"]
```

> 构造 volumes 路径说明（`nginx-unprivileged` 镜像与官方 `nginx` 镜像的差异）：

| 容器内路径 | 用途 |
|------------|------|
| `/etc/nginx/conf.d/*.conf` | 自动 include 进 `nginx.conf`，放下我们的 `default.conf` |
| `/etc/nginx/auth` | 自建目录，放 `.htpasswd`（官方镜像没有） |
| `/etc/nginx/certs` | 自建目录，放 TLS 证书 |

启动：

```bash
cd /etc/gpt-outlook-nginx
docker compose up -d
docker compose logs -f
```

---

## 7. 验证

### 7.1 容器内语法检查

```bash
docker exec gpt-outlook-nginx nginx -t
```

应输出 `syntax is ok` / `test is successful`。

### 7.2 命中 Basic Auth

无凭证访问应返回 `401 Unauthorized`：

```bash
curl -i https://your-domain.example.com:47279/
```

带凭证应透传到 WebUI：

```bash
curl -i -u 你的用户名:你的密码 https://your-domain.example.com:47279/
```

### 7.3 SSE 日志流验证

WebUI 的 `/api/.../stream` 类接口需要长连接，确认响应头里有 `Content-Type: text/event-stream` 且不被 Nginx 缓存：

```bash
curl -N -u 你的用户名:你的密码 https://your-domain.example.com:47279/api/log/stream
```

无输出但连接保持即正常（说明 `proxy_buffering off` 生效）。

### 7.4 公网无法直连 8765 复核

```bash
# 从另一台外网机器
curl -i --max-time 5 https://your-domain.example.com:8765/   # 期望 timeout / refused
```

---

## 8. 让证书续期后自动同步 + Nginx reload

我们用的是拷贝副本（§3.1），不是软链，所以 `certbot renew` 改的是 `/etc/letsencrypt/...`，**不会**自动更新 `/etc/gpt-outlook-nginx/certs/` 里的拷贝。必须用 deploy-hook 在续期成功后「拷贝 + 改权 + reload」三步合一。

放 hook 脚本：

```bash
sudo tee /etc/letsencrypt/renewal-hooks/deploy/refresh-gpt-outlook-nginx.sh <<'EOF'
#!/bin/bash
set -euo pipefail
DOMAIN=your-domain.example.com
SRC=/etc/letsencrypt/live/$DOMAIN
DST=/etc/gpt-outlook-nginx/certs

# 1. 拷贝新证书（-L 跟随软链读真实文件）
install -m 644 -o 101 -g 101 "$SRC/fullchain.pem" "$DST/fullchain.pem"
install -m 640 -o 101 -g 101 "$SRC/privkey.pem"   "$DST/privkey.pem"

# 2. 热加载 nginx（如容器没起，忽略错误）
docker exec gpt-outlook-nginx nginx -s reload || true
EOF
sudo chmod +x /etc/letsencrypt/renewal-hooks/deploy/refresh-gpt-outlook-nginx.sh
```

手动跑一次验证（不会触发真正续期，只在证书距过期 <30 天时才会真续）：

```bash
sudo /etc/letsencrypt/renewal-hooks/deploy/refresh-gpt-outlook-nginx.sh
# 或 dry-run 续期流程（会自动跑 hook）
sudo certbot renew --dry-run
```

> 不需要再改 `/etc/letsencrypt/renewal/your-domain.example.com.conf` 的 `deploy_hook` —— `/etc/letsencrypt/renewal-hooks/deploy/` 目录下的脚本会被 certbot 自动执行。

---

## 9. 修改密码 / 增删用户

```bash
# 覆盖重建
docker run --rm httpd:alpine htpasswd -bcB /dev/stdout 用户名 新密码 \
  | sudo tee /etc/gpt-outlook-nginx/auth/.htpasswd

# 追加用户
docker run --rm httpd:alpine htpasswd -bB /dev/stdout 新用户 新密码 \
  | sudo tee -a /etc/gpt-outlook-nginx/auth/.htpasswd

# 热加载（无需重启容器）
docker exec gpt-outlook-nginx nginx -s reload
```

---

## 10. 常见问题

### 10.1 容器启动后立刻退出

```bash
docker logs gpt-outlook-nginx
```

常见原因：
- `listen 80` 而非 `47279`：非 root 容器无法绑 <1024 端口，改 `listen 47279`。
- `.htpasswd` / 证书副本文件 UID 不是 101：`chown -R 101:101 /etc/gpt-outlook-nginx/{auth,certs}` 修正（见 §3.1 / §4.2）。
- `/etc/gpt-outlook-nginx/certs/` 里没拷到 `fullchain.pem` / `privkey.pem`：回 §3.1 用 `cp -L` 拷一份过来。
- `proxy_pass` 写成 `http://localhost:8765`：容器内 localhost 是容器自己，必须用 `host.docker.internal`。

### 10.2 Nginx 起来了但回源 502 / connection refused

- WebUI 没启动或还停在 `127.0.0.1`：`systemctl status gpt-outlook-webui`，并 `ss -lntp | grep 8765` 确认是 `0.0.0.0:8765`。
- `--add-host` / `extra_hosts` 没生效：进容器 `docker exec gpt-outlook-nginx getent hosts host.docker.internal` 应能解出宿主网关。
- 宿主防火墙挡了 docker 网段到 8765：见 §2，确认 `ufw allow in on docker0 to any port 8765`。

### 10.3 一直 401 但密码没错

`.htpasswd` 权限不对，worker（UID 101）读不到：

```bash
sudo chown 101:101 /etc/gpt-outlook-nginx/auth/.htpasswd
sudo chmod 640       /etc/gpt-outlook-nginx/auth/.htpasswd
docker exec gpt-outlook-nginx nginx -s reload
```

### 10.4 SSE 流被截断 / 日志不实时

没关 `proxy_buffering`，确认配置里有：

```nginx
proxy_buffering off;
proxy_read_timeout 24h;
```

reload 后 `curl -N` 验证。

### 10.5 改了 conf 不生效

```bash
docker exec gpt-outlook-nginx nginx -t          # 先校验语法
docker exec gpt-outlook-nginx nginx -s reload   # 再热加载
```

### 10.6 想去掉 Basic Auth 只留反代

把 `location` 块里的两行 `auth_basic*` 删掉或改成 `auth_basic off;` 即可。

### 10.7 8765 被公网直连绕过

如果发现 `https://your-domain.example.com:8765/` 在外网可访问，说明 WebUI 改 `0.0.0.0` 后没被防火墙挡。回到 [§2](#2-锁住-8765关键安全步骤) 把 8765 锁到 docker 网段。这是整个方案的 **唯一入口**保证，漏了它等于绕过 Basic Auth。

### 10.8 浏览器一直弹 Basic Auth 密码框

浏览器填了正确密码还是反复弹框 —— 链路是：

```
浏览器(Nginx Basic Auth 通过) ─▶ Nginx 回源 ─▶ WebUI 自己又鉴权失败(401) ─▶ Nginx 透传 401 给浏览器 ─▶ 浏览器以为 Basic 密码错 ─▶ 再弹
```

根因是 WebUI 的 `--token` 与 Nginx Basic Auth 冲突：浏览器在通过 Basic Auth 之后只会继续带 `Authorization: Basic xxx` 这一格头，而 WebUI 的 token 校验需要的是 `Authorization: Bearer xxx` 或自定义 header（取决于 WebUI 的实现），浏览器不可能自动加上，于是 WebUI 返回 401，被 Nginx 透传成弹框。

**判断是不是这个原因**：带 Basic + 显式带 token 头打 Nginx

```bash
curl -v -u 你的账号:你的密码 \
  -H "Authorization: Bearer 705d95a0a246172b8d39c2eeac44f204f07b2078d36c075ddd84b5a46096408d" \
  https://your-domain.example.com:47279/
```

加了 token 头能通 = 就是 token 拦的。

**修法**：WebUI 用浏览器访问时，**不要开 `--token`**（推荐）。Basic Auth 已经够用。systemd 的 `ExecStart` 去掉 `--token ...` 那一整行：

```bash
# 编辑后确认 --port 8765 这行末尾没有 \，Restart=on-failure 独立成行
systemctl daemon-reload
systemctl restart gpt-outlook-webui
curl -i -u 你的账号:你的密码 https://your-domain.example.com:47279/   # 不再弹
```

> 如果确实需要 `--token` 给脚本 API 调用，浏览器路径就用不了了 —— 两个认证不能叠加工作。可以只保留 Basic Auth，脚本侧直接绕过 Nginx 走 `127.0.0.1:8765 + token`。

### 10.9 WebUI 启动报 `unrecognized arguments: Restart=on-failure`

`ExecStart` 最后一行参数末尾多留了 `\` 续行符，把下一行的 systemd 指令 `Restart=on-failure` 当成 `start_webui.py` 的参数传进去。systemd 对 `\` 续行的处理是「合并到下一行」，所以不能漏删。

正确写法（注意 `--port 8765` 这行**没有** `\`）：

```ini
ExecStart=/root/.local/bin/uv run start_webui.py \
    --host 0.0.0.0 \
    --port 8765
Restart=on-failure
```

错误写法（❌ `--port 8765` 行尾有 `\`）：

```ini
ExecStart=/root/.local/bin/uv run start_webui.py \
    --host 0.0.0.0 \
    --port 8765 \
Restart=on-failure
```

改完 `daemon-reload && restart` 即可。**删任何一行参数时，记得同步清掉上一行末尾的 `\`**。

---

## 11. 快速检查清单

- [ ] systemd 服务已把 `--host` 改成 `0.0.0.0`、删掉 `--ssl-*`、删掉 `--token`（浏览器场景），并 `daemon-reload + restart`
- [ ] `ss -lntp | grep 8765` 确认监听 `0.0.0.0:8765`
- [ ] 防火墙只放行 `47279/tcp`（公网）和 docker 网段到 `8765`，公网直连 8765 应被拒
- [ ] `.htpasswd` 用 `htpasswd -nbB` 生成（bcrypt），且 `chown 101:101` `chmod 640`
- [ ] `/etc/gpt-outlook-nginx/certs/` 已用 `cp -L` 拷贝 `fullchain.pem` / `privkey.pem` 副本，并 `chown -R 101:101` `chmod 640 privkey.pem`
- [ ] `/etc/letsencrypt/renewal-hooks/deploy/refresh-gpt-outlook-nginx.sh` 存在且 `chmod +x`，`certbot renew --dry-run` 能跑通
- [ ] `default.conf` 里 `listen 47279 ssl`（不是 80，也不需要再做 `47279:8080` 端口映射改写）
- [ ] `proxy_pass http://host.docker.internal:8765;`
- [ ] `proxy_buffering off` + `proxy_read_timeout 24h`，保证 SSE
- [ ] `docker-compose.yml` 有 `extra_hosts: host.docker.internal:host-gateway`
- [ ] `docker exec gpt-outlook-nginx nginx -t` 通过
- [ ] 续期 deploy-hook 加了 `docker exec gpt-outlook-nginx nginx -s reload`
- [ ] `ExecStart` 最后一行参数末尾**没有** `\`，`Restart=on-failure` 独立成行（见 §10.9）
- [ ] 浏览器访问不再无限弹 Basic Auth 框（如弹，按 §10.8 删 `--token`）