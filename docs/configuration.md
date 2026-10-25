# WebUI 配置教程

在 AstrBot WebUI 的插件管理中，找到“科研最小实验”，打开插件配置。
页面只有两个配置项：**谁可以使用**和**实验配置**。
右侧的全屏按钮可以放大 JSON 编辑器。

## 第一步：设置谁可以使用

如果只给自己用，并且你已经是 AstrBot 管理员，直接留空即可。
这里的“管理员”指 AstrBot 识别的消息发送者管理员身份，不是仅登录了 WebUI。

需要允许其他用户使用时，在列表中新增一项，填入：

```text
["my-telegram","12345"]
```

将 `my-telegram` 替换为 AstrBot 中实际的平台 ID，`12345` 替换为该平台的用户 ID。
两项均按字符串填写，使用英文双引号；整行作为一个列表项。
不要只填用户 ID，也不要把包含多个身份的外层数组放进单个列表项。
平台 ID 与会话回复地址不是同一个字段；同一用户在不同平台上的权限分别配置。

## 第二步：填写实验配置

打开 [README 的最小配置](../README.md#最小配置)，复制完整 JSON 到“实验配置”。
保留默认的 `{}` 可以加载插件，但不能创建科研任务。

首次使用优先检查以下内容：

| 配置字段 | 含义 | 该怎么填 |
| --- | --- | --- |
| `participants` | 参与讨论的成员，以及各自的 harness 和模型 | 例如 `builder`、`reviewer`；名称由你决定 |
| `participants.<名称>.harness` | 该成员使用哪种本地程序 | `codex` 或 `deepseek` |
| `participants.<名称>.model` | 该成员实际调用的模型 | 填写明确可用的模型标识；示例中的 `YOUR_...` 必须替换 |
| `participants.<名称>.command` | 在本机启动 harness 的命令 | 使用参数数组；程序不在 PATH 中时填写绝对路径 |
| `participants.<名称>.role` | 该成员负责什么 | 可用中文，例如“实验设计与编程” |
| `assignments` | 各阶段由哪些成员负责 | 使用 `participants` 中已配置的名称，不填模型名称 |
| `agents_file` | 你自己的统一约束文件 | 可留空或填写已有文件的绝对路径；插件不生成或改写它 |
| `platform` | 本次使用哪个执行平台 | 必须与 `platforms` 中某个条目的名称一致，例如 `local` 或 `my_autodl` |
| `platforms` | 执行平台的具体连接配置 | 本地、普通 SSH 或 AutoDL，详见 README |
| `concurrency` | 同时推进的任务数量 | 默认 `1`；首次使用建议保留 |
| `limits` | 每个任务的资源上限 | 可先使用 README 的默认值，含义见下表 |

Windows 路径建议写成 `C:/research/AGENTS.md`。
如果使用反斜杠，JSON 中需要写成 `C:\\research\\AGENTS.md`。
启动命令中，程序和每个参数分别占数组的一项，不要把整条命令塞进一个字符串。

例如，以下表示“设计由两位成员讨论，编程由 builder 负责，最终由 builder 汇总”：

```json
{
  "research": ["reviewer"],
  "design": ["builder", "reviewer"],
  "implement": ["builder"],
  "analyze": ["reviewer"],
  "summary": ["builder"]
}
```

这段仅是 `assignments` 的值，不能替代完整实验配置。
如果暂时只配置一名成员，也可以让五个阶段都指向同一个成员。
五个阶段都必须配置；同一成员可以承担多个阶段。

资源上限采用以下默认值：

| `limits` 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `model_calls` | `30` | 插件发起的 harness 调用轮次上限，包含讨论、编程、修复和汇总 |
| `discussion_rounds` | `2` | 设计讨论轮数 |
| `wall_seconds` | `3600` | 任务活动时间上限，单位秒；不是单次实验的运行时间 |
| `run_seconds` | `600` | 单次实验进程的运行时间上限，单位秒 |
| `repair_rounds` | `5` | 程序或实现修复轮次上限；不用于自动调参追求正结果 |
| `network_retries` | `2` | 网络故障处理时允许的重试次数；不表示可重复提交结果不明的实验 |
| `archive_bytes` | `20000000` | 单次运行收集产物的大小额度，单位字节，约 20 MB |
| `tokens` | `0` | `0` 表示不启用按观测 token 用量停止；非零时也不是服务商侧精确硬上限 |

不要在此处填写 API Key、AutoDL 令牌、SSH 密码或私钥内容。
harness 认证由本机已安装的程序管理；AutoDL 凭据配置参见 [AutoDL 教程](autodl.md)。

## 第三步：先讨论，再授权实验

首次使用保留以下本地平台配置：

```json
{
  "platform": "local",
  "platforms": {
    "local": {
      "type": "local",
      "allow_execute": false
    }
  }
}
```

这段是完整配置中的平台部分。它允许先完成澄清与方案讨论；
即使批准了方案，也不会获得实验执行权限。
需要实际运行时，再检查账号、工作区和平台配置，明确启用对应的 `allow_execute`。
AutoDL 的查询、开机、关机还有独立权限，不能只打开一个执行开关。

保存配置后重新加载插件，使插件重新读取设置。然后通过已授权的消息账号发送：

```text
research new 比较我的改动与原有基线，先讨论实验方案
research list
research status TASK_ID
research plan TASK_ID
research models TASK_ID
```

`TASK_ID` 替换为新任务返回的编号。按 Bot 要求添加命令前缀。
`models` 查询任务中已有的发现与调用记录，不会额外发起付费验证。
收到澄清问题时用 `research reply TASK_ID 回答内容` 回复。
方案需要确认时，先查看内容与版本，再使用 `research approve TASK_ID 版本号`。
未回复不会被当作批准。

## 常见问题

| 现象 | 先检查什么 |
| --- | --- |
| 插件能加载，但不能创建任务 | 实验配置是否仍为 `{}`；是否配置了成员、明确模型和五个阶段分工 |
| 提示没有使用权限 | 发送者是否是 AstrBot 管理员，或者平台 ID 与用户 ID 是否都填写正确 |
| 提示找不到程序 | `command` 的第一项是否可执行；AstrBot 启动进程的 PATH 是否包含它 |
| Windows 上 DSH 无法启动 | 按 README 示例使用 `node.exe` 与 DSH 脚本的绝对路径，避开 `.ps1` 或 `.cmd` 包装脚本 |
| 模型显示在目录里，但调用失败 | 模型标识、登录状态和账号权限；被发现不代表已成功调用 |
| 讨论完成后无法运行 | 当前版本的方案是否已批准，平台 `allow_execute` 是否已启用 |
| AutoDL 实例明明存在，准备阶段却失败 | CLI、凭据类型、实例标识、查询权限和 SSH 配置，详见 AutoDL 教程 |
