# NeoQBot 资源编排指南

资源编排把账号、群和知识资源放在同一张可连接的画布中。它负责表达“谁管理谁、数据流向
哪里，以及某个 Bot 在某个群承担哪些事务”。QQ/飞书 Bot 的新增、删除、账号连接、登录和能力
配置全部在这个页面完成；系统设置只维护模型、共享策略、存储、运行时和安全等平台参数。
入群管理、消息记录与分析、公告同步等具体事务全部配置在 QQ Bot→QQ群连接上。

## 节点类型

| 节点 | 用途 | 点击后的详情 |
|---|---|---|
| QQ Bot | NapCat / OneBot 真人 QQ 账号 | 连接状态、扫码登录、单 Bot 详细配置、各群事务分工 |
| 飞书 Bot | 飞书 CLI 登录态与文档操作账号 | CLI 状态、登录/退出、单 Bot 详细配置 |
| QQ 群 | QQ 治理和消息采集范围 | 管理 Bot、逐 Bot 事务、消息、公告、风控分析、入群申请 |
| 飞书群 | 飞书协作或通知目标 | 平台标识、说明和连接关系 |
| 知识库 | 飞书、Notion、本地或其他知识资源 | Provider、访问地址、说明和连接关系 |

在空白画布右键，或点击工具栏的“新建资源”即可创建节点。新建操作先打开中心配置窗口，只有
“保存并应用”成功后节点才正式写入配置。拖动节点主体调整位置，从节点右侧圆形端口拖到另一个
节点创建连接。节点右键菜单可打开详细编辑、定位、启用/停用或删除；双击连线或在右侧检查器中
可以删除连接。

已有 Bot 的内部 ID 创建后不可修改。该 ID 同时绑定节点地址、OneBot Webhook、Secret 文件和
平台登录身份；显示名称可以自由修改。需要更换身份时，应新建 Bot、迁移连接并确认新账号登录，
再删除旧节点，避免把仍然有效的 NapCat 登录态误判成另一个账号。

## 连接语义

| 连接 | 建议使用场景 | 对当前 Runtime 的影响 |
|---|---|---|
| 管理 `manages` | Bot 负责群治理、消息和公告事务 | QQ Bot → QQ 群形成可配置事务的群级分工 |
| 监听 `observes` | Bot 只采集或观察群事件 | 同样形成群级分工；若启用管理动作，诊断会提示关系语义不匹配 |
| 归档 `archives_to` | 群或 Bot 将公告、记录写入飞书/知识库 | 连接到飞书 Bot 时可映射公告归档目标 |
| 检索 `searches` | Bot 使用飞书 Bot 或知识库回答管理员查询 | 连接到飞书 Bot 时可映射检索账号 |
| 同步 `syncs` | 表达通用同步或后续适配器的数据流 | 当前主要作为声明式关系保存 |

一个 Bot 可以连接多个群，多个 Bot 也可以连接同一个群。业务记录使用 `bot_id + group_id`
隔离，因此相同消息 ID 在不同 Bot 下不会冲突。每条有效的 QQ Bot→QQ群连接都有独立 `tasks`：

```yaml
orchestration:
  edges:
    - id: recorder-observes-main
      source: qq-bot:recorder
      target: qq-group-main
      relation: observes
      enabled: true
      tasks:
        message_detection:
          record_only: true
          interval_minutes: 60
    - id: moderator-manages-main
      source: qq-bot:moderator
      target: qq-group-main
      relation: manages
      enabled: true
      tasks:
        join_management:
          execute_management: true
          minimum_confidence: 0.92
        message_detection:
          analyze: true
          handle: true
          interval_minutes: 15
```

依赖项由配置模型补齐，例如 `analyze: true` 会同时启用消息事务与轮询检测；这只影响当前连接，
不会修改该 Bot 连接的其他群。

## 典型拓扑

### 单账号管理多个群

```text
QQ Bot ──管理──> 社区主群
      ├─管理──> 审核群
      └─监听──> 公告群
```

### 多账号协作同一群

```text
记录 Bot ──监听──> 社区主群 <──管理── 审核 Bot
```

在“社区主群”的连接上，记录 Bot 可以只开启“纯记录群消息”，审核 Bot 则开启入群审核或消息
分析。两个账号使用独立 Webhook、Token 和审计身份；它们连接其他群时可以采用完全不同的事务。

### 公告归档与知识检索

```text
QQ Bot ──管理──> QQ 群 ──归档──> 知识库
   └────归档──> 飞书 Bot
   └────检索──> 飞书 Bot
```

## 旧配置迁移

加载旧版配置时，后端会在校验前读取每个 QQ Bot 的 `managed_group_ids` 与 `tasks`：

1. 为尚不存在的群号创建 QQ 群节点；
2. 为 Bot 和群创建 `manages` 连接；
3. 把原有 Bot 级 `tasks` 深拷贝到该 Bot 的每条 QQ 群连接，确保旧行为不丢失；
4. Webhook、Token 和管理员配置继续保留在资源编排的单 Bot 详细配置中；
5. 再次保存时只写入 `orchestration.resources`、`orchestration.edges[].tasks` 和
   `orchestration.layout`，不再持久化旧字段。

若旧版启用了事务却没有任何可迁移的群连接，配置会明确拒绝加载并提示先补充群归属，避免把有
副作用的任务静默扩散到未知范围。新配置中，编排边是唯一权威来源，不再维护 Bot 级镜像字段。

## 保存与并发修改

GUI 每次读取配置时都会取得一个修订号。保存时如果其他标签页或管理员已经更新配置，服务端
返回 `409 Conflict`，当前页面会提示刷新，而不会覆盖较新的配置。系统设置和资源编排使用不同
的保存接口：前者无法覆盖 `qq`、`feishu` 或 `orchestration`，后者也无法覆盖模型、策略、存储和
系统安全配置。离开有未保存修改的设置页或编排页时，浏览器也会要求确认。

OneBot 上游可能仍会把所有群事件发送到 Webhook。NeoQBot 在鉴权和 JSON 校验后立即按
`bot_id + group_id` 查找有效编排连接；未编排群返回 `reason: unmanaged_group`，不会进入运行时
队列、不会写数据库，相关记录只保留为 DEBUG 级别诊断信息。

## 操作建议

- 先创建和配置 Bot，再创建群和知识库节点；
- 连接 QQ Bot 与 QQ 群后，在任一端的右侧检查器打开“群内事务分工”逐群配置；
- 使用搜索框按名称、内部 ID、群号或节点类型快速定位；
- 大量导入旧群号后先使用“自动布局”，再人工调整重点链路；
- 首次真实运行保持 `dry_run: true`，确认群详情中的消息、公告与分析记录正确；
- Bot ID 创建后不可修改；更换身份时新建 Bot 并迁移连接，删除飞书 Bot 时 GUI 会清理群级引用；
- 知识库连接当前首先用于拓扑和配置表达，真正的数据写入仍取决于对应飞书 CLI 或后续适配器。
