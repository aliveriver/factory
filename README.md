# AstrBot 科研最小实验插件

这是一个独立的 AstrBot 插件，用于完成最小规模的科研对比实验。AstrBot 将
`research` 命令直接交给插件，插件负责维护任务状态、本地 harness 会话、协作讨论、
方案确认、执行证据和通知。普通调度与状态查询不调用聊天模型。

当前实现覆盖设计阶段 A 到 C。真实科研调优和阶段 D 的评估留待后续开展。

## 安装

通过 AstrBot 插件管理安装 `https://github.com/aliveriver/factory`，或者将本仓库文件复制到
`<AstrBot 数据目录>/plugins/astrbot_plugin_research`，然后在 AstrBot 中重新加载插件。
插件入口和元数据位于仓库根目录。

使用 AstrBot 已有的 Python 环境，可在 AstrBot 仓库根目录运行 `uv sync`。
插件使用宿主已有的 `filelock` 和 `psutil`，不额外引入 Python 依赖。当前宿主要求 Python 3.12+，
插件和远端进程监督程序的语法兼容 Python 3.10+。请自行在本机安装并完成各个 harness
的认证。远端不需要安装 harness，插件也不会安装。

需要 AstrBot 4.10.4 或更新版本。在插件设置中使用可视化表单：通过下拉框添加
Codex 或 DeepSeek 成员，填写模型、勾选负责阶段，再选择实验平台。
不要将令牌、密码或私钥内容写入配置。
SSH 使用密钥**文件引用**或 SSH agent；AutoDL 使用指定的环境变量引用或已有系统凭据存储。

第一次配置请先看 [WebUI 配置教程](docs/configuration.md)，其中逐项解释了页面上的字段、
必填内容和常见错误。使用 AutoDL 时，再按 [AutoDL 安装与使用教程](docs/autodl.md)
完成本机 CLI、凭据、SSH 与实例权限配置。

仅 AstrBot 管理员和在“谁可以使用”卡片中明确配置的用户可以使用插件。
平台 ID 和用户 ID 分别填写，不需要编写 JSON；旧版 `allowed_users` 仍兼容。
任务归属同时校验经过认证的平台标识和发送者标识，不依赖会话回复地址判断身份。

## 可视化配置入门

1. 打开 WebUI → 插件 → 科研最小实验 → 配置。新用户可保持“配置来源”为自动；
   旧 JSON 用户先填好表单，再明确选择“可视化表单”。
2. 添加 Codex 或 DeepSeek 成员，填写唯一名称与明确模型，勾选负责阶段。
   只用一名成员时可承担全部五个阶段；多名成员可按发言顺序讨论。
3. 平台默认按实验需求自动选择，优先顺序为本机 → SSH → AutoDL，可通过三个下拉框调整或排除平台；
   也可以固定使用一个平台。填写已确认的平台能力与环境说明，未知条件会先询问。
   权限默认关闭，仅讨论时无需开启执行权限。
4. AutoDL 选择“按实验方案自动创建，用完自动关机”，逐项添加允许使用的资源档位，
   再按需授予查询、付费创建、关机与 SSH 执行权限。
5. 保存并重新加载插件，再发送 `research new 你的想法`。方案获批后才会运行实验或创建实例。

模型、GPU、镜像与地域填写你实际可用的标识，不提供未经核实的现货或模型列表。
启动参数逐项输入，八项任务资源限制均有独立的“无上限”开关。
通用 AGENTS 与各平台专用 AGENTS 分开指定，文件均由用户在本机维护。
SSH、AutoDL 只运行实验，本机 harness 负责研究、设计、编程与分析，不在远端安装 harness。
完整步骤见 [可视化配置教程](docs/configuration.md)。

## 旧版 JSON 最小配置（高级兼容方式）

日常配置优先使用表单。只有选择“旧版 JSON”，或“自动”且旧 JSON 非空时，
页面底部的 `settings_json` 才会生效。升级保留其原文，不与新表单叠加权限。
两套配置不自动转换；原有自定义平台和高级字段可继续使用此方式。

将示例中的模型和命令替换为你明确选择且实际可用的配置。插件不提供默认模型，
也不会在调用失败后静默切换模型。

```json
{
  "participants": {
    "builder": {
      "harness": "codex",
      "model": "YOUR_EXACT_CODEX_MODEL",
      "command": ["codex", "app-server"],
      "web_search": "live",
      "role": "编写实现与最终汇总"
    },
    "reviewer": {
      "harness": "deepseek",
      "model": "[\"YOUR_PROVIDER\",\"YOUR_MODEL\"]",
      "command": ["dsh", "--profile", "acp"],
      "role": "资料检索与实验公平性审查"
    }
  },
  "assignments": {
    "research": ["reviewer"],
    "design": ["builder", "reviewer"],
    "implement": ["builder"],
    "analyze": ["reviewer"],
    "summary": ["builder"]
  },
  "agents_file": "/absolute/path/to/your/AGENTS.md",
  "platform": "local",
  "platforms": {
    "local": {
      "type": "local",
      "allow_execute": false
    }
  },
  "concurrency": 1,
  "limits": {
    "model_calls": 30,
    "discussion_rounds": 2,
    "wall_seconds": 3600,
    "run_seconds": 600,
    "repair_rounds": 5,
    "network_retries": 2,
    "archive_bytes": 20000000,
    "tokens": 0
  }
}
```

Windows 下应明确使用 Node 启动 DSH，避免依赖 PowerShell 或批处理包装脚本：

```json
["C:/path/to/node.exe", "C:/path/to/npm/node_modules/@deepseek-ai/dsh/lib/bin.js", "--profile", "acp"]
```

首批已验证的适配对象是 Codex app-server JSON-RPC 和官方 `@deepseek-ai/dsh` 的
ACP 配置。DeepSeek 的模型选择器是**不应自行解析或改写的 ACP 值**，通常表现为
编码成字符串的 JSON 数组。请从当前配置实际返回的 `configOptions` 中复制取值，
不要将显示名称直接当作模型选择器。

插件分别记录模型是否已配置、已发现和已成功调用。模型出现在目录中不代表当前账号
有权调用。任务的 `sessions` 保存最近一次发现和调用状态；调用失败会使对应的验证状态失效。
插件不会反复发送需要付费的验证请求。

`agents_file` 为可选项。插件读取适用的祖先目录 `AGENTS.md` 和用户明确指定的文件，
不会创建、覆盖 AGENTS，也不会把任务目标、分工或指标写入其中。
每位参与者在自己的轮次开始前都会收到相同的约束文本和文件指纹。
继续任务前会重新读取变更，并在任务中保存实际应用的版本。
任务详情单独存放在 `task.json` 和 `experiment.md`。

## 命令

如果 Bot 配置要求命令前缀，请在以下命令前添加相应前缀。将 `TASK_ID`
替换为实际任务编号：

```text
research new 比较我提出的改动与现有基线……
research list
research status TASK_ID
research reply TASK_ID 对当前澄清问题的回答
research supplement TASK_ID 想法变更或补充材料
research approve TASK_ID 1
research reject TASK_ID 1
research stop TASK_ID
research continue TASK_ID
research artifacts TASK_ID
research plan TASK_ID
research models TASK_ID
research configure TASK_ID {"limits":{"repair_rounds":7}}
research configure TASK_ID {"assignments":{"summary":["reviewer"]}}
research configure TASK_ID {"limits":{"run_seconds":null}}
```

`configure` 按名称选择管理员已配置的参与者，例如
`{"participants":["builder","reviewer"]}`，并可覆盖任务的阶段分工和资源限制。
任务资源限制用 `null` 表示无上限；原有整数 `0` 的含义保持不变。
用户不能通过消息加入任意启动命令、模型、凭据、文件系统根目录或执行平台。
修改配置时任务必须处于非活动状态；参与者或分工变化会使此前的方案批准失效。

每版方案记录基线、用户改动、数据与条件、主要指标、最小规模、判定规则、
资源策略、实际参数列表（argv）和必需证据。批准绑定当前方案的准确版本。
用户没有回复时继续等待；旧回复不能批准替换后的方案。
第一版实现对所有方案都要求确认，包括想法已经清晰的任务。
这一默认流程不要求用户提前估算 token 或算力预算。

问题会关联到已记录的任务和参与者会话。harness 原生提问与权限请求属于不同事件类型。
当前版本可通过 `research reply TASK_ID deny` 拒绝原生工具执行提权请求，
也可停止任务；不接受 `allow`。实验执行必须另外满足方案已获批准和管理员已授予平台权限。

核心想法的修改必须在任务非活动时进行，并生成新方案。已完成、已拒绝或已取消的任务
保留归档；需要开展新工作时请新建任务。暂停会保留诊断信息和当前计数。
如果资源限制已耗尽，可先通过 `configure` 提高对应上限，再继续任务。
“未观察到提升”和“证据不足”不会自动触发调参或追加实验。

## Harness 与执行边界

harness 在本机运行。Codex 使用只读沙箱，并禁用 shell、代码模式、应用、插件、
hooks、浏览器或计算机操作、MCP 服务和任务委派。
插件在用户配置的 ACP 配置之后追加 `dsh-research.patch.yml`，禁用本地 shell、
文件系统、工作流、goal 和子 agent 工具。公网搜索与抓取仍通过已安装 harness
自身的网页工具提供。搜索是否成功以实际工具事件为依据，仅打开配置开关不算验证成功。

harness 通过结构化的 `files` 字段返回实现内容。插件只在任务专用工作区内写入
相对路径文件，拒绝越界路径、符号链接和 AGENTS 写入，并在运行前检查 Python 语法。
多个写入参与者按顺序执行。平台操作、文件修改和资源限制均由插件统一控制。

`allow_execute` 默认为 false。启用后，允许使用配置的操作系统账号运行已明确批准的
实验代码。本地和 SSH 进程监督程序负责超时与进程控制，**不能作为运行恶意 Python
代码的安全沙箱**。请为可信科研代码选择合适的账号与工作区。
本地实验进程仅接收精简后的操作系统环境，不继承模型或 API 凭据环境变量。

`model_calls` 统计由插件发起的 harness 轮次，包括检索、讨论、编程、修复、分析和汇总。
harness 内部可能发生协议未公开的服务商调用。实际用量、缓存用量、估算值和未知值分别记录；
DSH ACP 的上下文占用不等于计费用量。可选的 `tokens` 是根据已观测用量触发停止的阈值，
不是服务商侧的精确硬上限。即使用量总数不可获得，调用次数、讨论轮次、
任务活动时长和实验运行时长仍受限制。

非正常重启后，从最近活动检查点起经过的墙钟时间会保守计入资源消耗，
并单独记录为 `recovery_wall_seconds`；重启 Bot 不会重置任务时间额度。

## SSH 执行平台

以下配置适用于已明确授权上传文件和运行实验的服务器：

```json
{
  "type": "ssh",
  "host": "AUTHORIZED_HOST",
  "user": "researcher",
  "port": 22,
  "identity_file": "/private/path/id_ed25519",
  "root": "/home/researcher/dedicated-astrbot-experiments",
  "python": "python3",
  "allow_execute": true
}
```

SSH 目标需要提供 POSIX shell、Python 3.10+ 和 SCP 支持。客户端使用 OpenSSH、
非交互认证、已有的受信任主机密钥，以及对 POSIX 路径进行引用的传统 SCP 协议。
连接时禁用用户 SSH 配置，并固定已解析的地址。每次运行使用独立目录。
上传排除本地运行数据并拒绝符号链接；下载设置大小上限，并比对远端和本地的 SHA-256。
日志和产物按每次运行分别保留。

进程监督程序以普通 Python 文件复制到远端，不安装 harness 或第三方依赖。
它记录标准输出、标准错误、进程状态和退出码，实施运行期限，并监测取消标记文件。
SSH 断开不代表进程失败。Bot 先持久化提交意图，在重连或重启后查询原来的运行记录。
启动结果不明确时暂停任务，不重复提交。模型工作暂停期间，监控循环仍可监督已有进程，
但不会提交新实验。

**该执行器禁止访问共享服务器 `10.8.176.151`**，也禁止访问解析到该地址的目标。
通用预检、上传、运行、查询、取消、收集、修复、恢复和清理流程都不能访问它。
另行授权的一次登录与建目录测试不属于已安装插件的功能。
2026 年 10 月 24 日的测试仅执行了以下命令：

```sh
ssh duankaidi@10.8.176.151 'mkdir -- "$HOME/astrbot-plugin-login-test-20261024"'
```

该命令退出码为 0，标准输出和标准错误均为空。这只证明登录与建目录命令成功。
目录予以保留，没有获得后续命令或清理授权。其他 SSH 验收均来自受控或离线测试，
不代表已在该共享服务器上验证。

## AutoDL 适配

使用用户指定的 [saurlax/autodl-cli](https://github.com/saurlax/autodl-cli)。
支持两种用法：按已批准的实验资源需求自动创建 Pro 实例，或使用你已有的实例。
自动创建模式下，插件将“资源档位选择 → 方案批准 → 创建并等待启动 → SSH 实验 →
结果归档 → 自动关机”串成完整流程。失败、取消或暂停时也会尽力归档并关机。
关闭的是计算实例，不会释放实例或删除数据。

管理员先配置允许使用的 GPU、镜像和地域档位，harness 根据实验需求选择最小适用档位，
并在方案中说明理由；插件记录确切规格，批准后才允许创建。
插件不会让模型凭空决定任意 GPU、镜像或费用。它也不承诺资源现货或价格，
当前档位不满足需求时应说明阻塞，不能偷偷扩大资源。

配置方法与完整示例见 [AutoDL 安装与使用教程](docs/autodl.md#4-配置插件)。
自动创建仅适用于 Pro；普通容器缺少可用的创建接口，仍只能使用已有实例。
新实例镜像需预置所需环境和 SSH 公钥。CLI 读取的连接详情只保留主机与端口，
不保存密码或 Jupyter 令牌。

以下为**已有实例模式**的兼容配置，未设置 `mode` 时也按此模式处理。
实例生命周期管理与 SSH 实验执行分别控制：

```json
{
  "type": "autodl",
  "ssh": {
    "host": "AUTHORIZED_INSTANCE_HOST",
    "user": "root",
    "port": 22222,
    "identity_file": "/private/path/instance-key",
    "root": "/root/dedicated-astrbot-experiments",
    "allow_execute": true
  },
  "autodl": {
    "command": ["autodl"],
    "mode": "existing",
    "kind": "pro",
    "instance": "YOUR_EXISTING_INSTANCE_UUID",
    "token_env": "MY_PRIVATE_AUTODL_TOKEN",
    "allow_discovery": false,
    "allow_start": false,
    "allow_stop": false,
    "start_on_prepare": false,
    "running_status": "running",
    "stop_policy": "leave"
  }
}
```

`kind` 可选 `pro` 或 `container`；上游 CLI 对普通容器的支持仍为实验性质。
两者分别使用开发者凭据和网页凭据。拥有凭据不等于已授予生命周期操作权限。
实例发现与状态查询、创建、启动、关机分别授权。插件不提供释放实例操作。
自动创建模式需要明确允许创建、查询、关机和 SSH 执行；
恢复一个已关机的任务实例还需允许启动。

适配器提供经过脱敏的列表、状态查询和明确的启动、关机调用。
任务准备阶段检查实例状态，仅在 `start_on_prepare` 和 `allow_start` 同时启用时
按需启动。启动意图持久化；响应不明确时先查询，不盲目重复启动。
已有模式下 SSH 地址单独配置。自动创建模式会通过 `instance get` 获取新实例的地址，
只保留 `proxy_host` 和 `ssh_port`，其余可能含凭据的字段不写入任务或会话记录。

`stop_policy` 可选 `leave`、`after_archive` 或 `always`。
此选项用于已有实例；插件自动创建的实例固定按 `always` 收尾，并确认平台关机状态。
`always` 在归档失败时仍尝试执行已单独授权的关机操作；
`after_archive` 在归档不完整时保留实例运行，并记录该状态以便人工处理。
关机意图持久化；结果不明确时先查询，再决定后续操作。
任务状态、实验进程状态和实例状态分别记录，连接失败或关机失败时也不混为一谈。

本次实现的真实验证未获得 AutoDL CLI、实例凭据或生命周期操作授权。
协议契约测试仅使用注入的传输替身。真实实例发现、启动、关机、SSH 访问和计费行为
均尚未验证。

创建前先保存唯一名称与提交意图。创建响应不明确时，按名称分页查询并绑定原实例，
不会再次创建；找不到或出现多个匹配时暂停并提示人工核对。重启后即使尚未提交实验，
也会恢复这条核对和关机流程。关机响应不明确时仅查询，不盲目重发。
关机未确认时任务不报告完整完成。

自动关机依赖本机 Bot 正常运行及 AutoDL 接口可达；Bot 长时间离线时不能承诺平台停止计费，
重启后会继续核对。关机后的存储费用以平台规则为准，插件不把关机等同于免费或释放。

## 状态、恢复与产物

运行数据通过 `StarTools.get_data_dir("astrbot_plugin_research")` 定位，
实际委托给当前 AstrBot 版本的 `astrbot.core.utils.astrbot_path`。
该版本没有仓库指导中提到的旧 `path_utils` 模块。

```text
<插件数据目录>/<任务 UUID>/
  task.json                 # 原子写入的版本化记录与通知待发送队列
  experiment.md             # 独立保存的当前实验约定与结果
  transcripts/              # 各阶段原始输出与约束指纹
  workspace/                # 结构化实现文件
    .runs/<运行 UUID>/      # 请求、状态、取消标记与原始日志
      source/               # 版本化的本地执行副本及其输出
      archive/              # 不可变的本地证据快照
      download/             # 下载的 SSH 证据
```

记录包括原始想法与补充、任务归属和回复地址、版本化批准历史、分工与会话引用、
资源计数、提交标识、代码与约束指纹、复现命令参数、产物清单和结果。
凭据留在外部引用位置。

重启会暂停被中断的模型工作，不会重放全部历史，也不会假定实验已停止。
已有运行按标识核对状态。进程监督程序独立于 Bot 实施运行期限；
后台监控获取终态证据，并执行已配置的实例策略。
`continue` 从已记录的阶段和相同 harness 会话引用继续。
传输重启后，原生请求标识会失效，因此应回复恢复后的会话，而不是旧权限请求。

缺失或损坏的下载保持 `not_saved` 状态；有意仅保留远端引用的大文件会在清单中明确标记，
不会冒充本地归档。必需证据不完整时暂停最终交付。
继续归档阶段只重试收集，不重新运行实验或模型分析。
通知失败会保留在待发送队列中；即使平台不支持主动消息，
`status` 和 `artifacts` 仍可查询。不自动删除文件或远端目录。

## 验证

准备一个已安装依赖的 AstrBot 开发工作区，在其仓库根目录运行本插件独立仓库中的测试：

```sh
uv run python -m pytest /absolute/path/to/factory/tests -q
```

测试会按插件安装名加载本仓库，不依赖克隆目录的名称，并使用开发环境中的真实 AstrBot
宿主模块。在插件仓库根目录使用 Ruff 检查格式与代码：

```sh
ruff format .
ruff check .
```

离线测试明确标注 harness、RPC 和 SSH/AutoDL 传输替身。
测试覆盖真实插件入口加载、任务归属、绕过默认聊天模型、持久状态、
澄清与批准及拒绝、双参与者交接、AGENTS 保持不变、限制与修复、
取消、恢复、归档错误和实例策略。另有受控测试实际运行本地 Python
进程监督程序，这些测试不使用 SSH。

真实 harness 验证须主动运行，会消耗模型调用。请提供私有设置 JSON，
明确填写两名真实参与者及其分工：

```sh
cd /absolute/path/to/AstrBot/data/plugins
/absolute/path/to/AstrBot/.venv/bin/python -m astrbot_plugin_research.verify_harnesses \
  --settings /private/path/research-settings.json \
  --output /private/path/local-harness-acceptance
```

Windows 下使用宿主环境的 `.venv/Scripts/python.exe`。
该模块命令要求已安装目录名为 `astrbot_plugin_research`。

此验证检查真实澄清、双参与者讨论与汇总、显式模型选择、AGENTS 指纹不变、
会话持久化、取消与继续，以及真实网页工具事件，并在本地保存 JSON 和会话记录。
验证固定禁用实验执行，始终不批准测试方案，不联系 SSH/AutoDL，
也不用于衡量科研改动是否有效。

2026 年 10 月 24 日的本机真实探测使用
`codex-cli 0.163.0-alpha.10.2` 和 `@deepseek-ai/dsh 0.2.1-rc.2`。
两个真实 harness 均完成多轮输入、跨进程会话恢复、取消、继续输入和实际搜索工具调用。
Codex 与 DeepSeek 交换了设计意见，由指定的 Codex 参与者汇总出带版本的方案，
并停留在等待批准状态。这些属于集成测试；受控本地执行和离线 SSH/AutoDL
测试是另外的验证证据。

本次实现验证的运行平台为 Windows x86_64。
所有插件文件均通过 Python 3.10 语法检查；macOS、Linux 和 Arm64
仍需各自的实际运行验证。

协议参考资料：

- [Codex app-server](https://developers.openai.com/codex/app-server)，以及已安装二进制生成的 JSON Schema。
- [官方 DeepSeek Harness ACP](https://github.com/deepseek-ai/deepseek-harness/tree/master/packages/acp/acp)，以及已安装的配置与软件包文档。
- [AutoDL CLI](https://github.com/saurlax/autodl-cli)，包括 JSON 响应结构、命令参考、生命周期和认证源码契约。
