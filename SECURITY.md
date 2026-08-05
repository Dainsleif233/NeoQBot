# Security Policy

[简体中文](#安全策略) · [English](#english)

## 安全策略

### 私下报告漏洞

请不要在公开 Issue、讨论区、日志或截图中披露尚未修复的漏洞、Token、Cookie、二维码或账号登录态。
请通过 GitHub 的
[Private vulnerability reporting](https://github.com/LYOfficial/NeoQBot/security/advisories/new)
提交报告，并尽量包含：

- 受影响版本或提交、部署方式和相关配置；
- 可重复的最小步骤、影响范围和必要的概念验证；
- 已采取的临时缓解措施；
- 可安全联系你的方式。

维护者会先确认报告，再协调修复和披露时间。请在修复发布前避免公开细节。仅当前默认分支和最新正式
版本接受安全修复；旧版本应先升级后再验证。

### 安全部署基线

NeoQBot 无法承诺“绝对零漏洞”。安全部署必须同时依赖应用配置、主机防火墙、反向代理、容器运行时
和 QQ/飞书账号自身的安全控制。最低要求如下：

1. 保持 NeoQBot 管理端绑定 `127.0.0.1:6688`。NapCat WebUI `6099` 和 OneBot HTTP `3000`
   默认仅在 Compose 内部网络可达，不要为它们增加宿主机或公网端口映射。
2. 远程管理优先使用 WireGuard/Tailscale 等 VPN 或 SSH 隧道。需要域名访问时，使用受维护的 HTTPS
   反向代理，并在代理层增加来源 IP、身份感知访问或客户端证书控制。
3. 生产配置设置 `app.environment: production`、`app.require_https: true`、
   `gui.secure_cookie: true`，并把实际域名加入 `app.allowed_hosts`。
4. `app.forwarded_allow_ips` 只填写真正反向代理的地址；不要使用 `*`。将允许访问管理端的客户端
   IP/CIDR 写入 `app.management_allowed_networks`。代理转发来源地址时必须覆盖而不是盲目信任外部
   `X-Forwarded-*` 头。
5. 保持 `app.expose_api_docs: false` 和 `gui.allow_sensitive_settings_edits: false`。首次联调保持
   `app.dry_run: true`，确认鉴权、目标群和审计记录后再逐项启用写操作。
6. 使用 `neoqbot init-secrets` 或 Compose 初始化生成 Secret。管理 API、OneBot、NapCat WebUI、GUI
   密码不得复用；不要写入镜像、仓库、Issue、终端录屏或集中日志。
7. 限制 `data/`、数据库、消息归档、NapCat/飞书登录态和备份的文件权限。备份应加密，并测试恢复与
   删除流程。宿主机和镜像应持续安装安全更新。
8. QQ/飞书使用专用、最小权限账号，启用平台提供的二次验证和登录提醒。不要让 Bot 账号拥有不必要
   的服务器 shell、云平台或组织管理员权限。
9. `/healthz` 和 `/readyz` 只返回最小状态，但仍建议仅向负载均衡器或监控网络开放。应用内限流是
   单进程的最后一道防线；生产环境还应在反向代理或防火墙层配置连接数、请求速率和请求体限制。

一个最小的 Nginx 上游配置示例：

```nginx
location / {
    proxy_pass http://127.0.0.1:6688;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header X-Forwarded-For $remote_addr;
    client_max_body_size 2m;
}
```

TLS 终止、访问控制和限流应配置在该 `location` 或更外层的网关中。不要把示例视为完整的 Nginx
安全配置。

### Secret 泄露或异常登录处置

1. 立即从防火墙或反向代理隔离管理端、NapCat WebUI 和 OneBot 端口，暂停自动写操作。
2. 保存必要审计日志和时间线，但不要继续传播原始 Secret、Cookie、二维码或会话目录。
3. 分别轮换管理 API Token、OneBot Token、NapCat WebUI Token、GUI 管理员密码、模型密钥和飞书
   凭据；在 QQ/飞书平台撤销异常会话并检查登录设备。
4. 重启相关服务，确认旧 Token 和旧 Session 已失效，再检查审计记录、群公告、群成员操作和服务器
   持久化项是否被篡改。
5. 从可信版本重新部署；如果宿主机权限可能失陷，应重建主机而不是只更换应用密码。

## English

### Reporting a vulnerability privately

Do not disclose unpatched vulnerabilities or credentials in public issues, discussions, logs, or
screenshots. Use GitHub
[Private vulnerability reporting](https://github.com/LYOfficial/NeoQBot/security/advisories/new)
and include the affected revision, deployment details, minimal reproduction, impact, and temporary
mitigations. Coordinate disclosure with the maintainers. Security fixes target the default branch and
the latest release; upgrade unsupported versions before validation.

### Deployment baseline

No software can guarantee zero vulnerabilities. A secure NeoQBot deployment also depends on the host
firewall, reverse proxy, container runtime, backups, and QQ/Feishu account controls.

- Keep the console on `127.0.0.1:6688`. NapCat `6099` and OneBot `3000` are internal-only by
  default; do not publish them on the host or Internet.
- Prefer a VPN or SSH tunnel. If a domain is required, use an HTTPS proxy with an additional access
  control layer.
- In production, enable `app.require_https` and `gui.secure_cookie`; configure exact allowed hosts,
  trusted proxy addresses, and management client CIDRs. Never trust proxy headers from `*`.
- Keep API docs and sensitive GUI settings disabled. Start with dry-run mode and least-privilege bot
  accounts.
- Generate independent secrets, restrict persistent-data permissions, encrypt backups, patch the host
  and images, and add proxy/firewall rate limits. The built-in limiter is process-local defense in depth.

If credentials or a host may be compromised, isolate the ports, preserve safe audit evidence, rotate
every affected credential, revoke QQ/Feishu sessions, verify account and group changes, and redeploy
from a trusted revision. Rebuild the host if operating-system compromise cannot be excluded.
