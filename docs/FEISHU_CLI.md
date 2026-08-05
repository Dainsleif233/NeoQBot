# 飞书 CLI 适配契约

飞书 CLI 面向 AI Agent，发布节奏和命令结构可能变化。NeoQBot 将可执行文件与参数模板放在
配置中，核心服务只依赖下面两个稳定动作。

## 安全执行规则

- `feishu.executable` 是唯一可执行文件；
- `command_templates` 中每项都是一个独立参数；
- 不启动 shell，不支持管道、重定向、`&&` 或变量展开；
- stdout 最多按当前进程内存读取，非零退出码视为失败；
- stderr 只把末尾 2000 字符写入同步错误，避免无限日志；
- 登录态目录应只授予运行 NeoQBot 的系统用户访问。

## archive_announcement

可用占位符：

| 占位符 | 内容 |
|---|---|
| `{payload_json}` | 完整公告 JSON |
| `{announcement_id}` | QQ 公告 ID |
| `{group_id}` | QQ 群号 |
| `{title}` | 公告标题 |
| `{content}` | 公告正文 |
| `{author_id}` | 发布者 QQ |
| `{published_at}` | ISO 8601 发布时间或空串 |

建议飞书表格至少建立这些列：`group_id`、`announcement_id`、`title`、`content`、
`author_id`、`published_at`、`archived_at`。如果当前 CLI 不支持直接新增电子表格行，可以写一个
很薄的本地包装器，把 `{payload_json}` 转换为该版本 CLI 所需的多次参数；包装器仍应使用
argv 调用官方 CLI，不要启用 shell。

长公告可能超过 Windows/Linux 的命令行长度。如果 CLI 或包装器支持从标准输入读取 JSON，
设置 `feishu.archive_payload_stdin: true`，并在命令模板中使用该工具对应的 stdin 参数（例如
`--json -`）。NeoQBot 会把完整 `{payload_json}` 写入子进程 stdin。

## search

可用占位符为 `{query}`、`{limit}`。命令标准输出应为以下任一形式：

```json
[
  {"title": "文档标题", "snippet": "命中的内容片段", "url": "https://..."}
]
```

或：

```json
{"items": [{"title": "文档标题", "snippet": "片段", "url": "https://..."}]}
```

数组字段也可命名为 `data` 或 `results`。如果 CLI 只能输出普通文本，NeoQBot 会将整段文本
作为一个结果回复，但建议使用 JSON 以保证稳定。

## 登录态

在宿主机或镜像内按官方页面完成真人账号登录，再把 CLI 登录态目录挂载到容器的
`/home/neoqbot` 对应路径。不要把 cookie、refresh token、个人配置目录打进公开镜像。登录态过期
时，公告会继续保存在 SQLite 并标为 failed；重新登录后下一轮自动补传。
