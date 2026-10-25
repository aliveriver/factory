# AutoDL 安装、配置与使用

插件使用 `saurlax/autodl-cli` 按需创建 Pro 实例或管理已有实例，通过 OpenSSH 上传和运行实验。
这两条连接独立配置：**AutoDL CLI 管理实例，SSH 连接实例里的实验环境**。
插件不会自动安装 CLI，不会释放实例，也不会在远端安装 harness。
自动创建需先配置允许使用的资源档位，方案获批后才会创建；结束、失败或取消时自动关机。
普通容器仅支持已有实例，不支持自动创建。

## 1. 在本机安装 CLI

从 [上游 Releases](https://github.com/saurlax/autodl-cli/releases) 下载与你的操作系统和
架构相符的压缩包，按发布页提供的 `checksums.txt` 校验后解压。
Windows x86_64 使用 Windows amd64 包。

例如，将 `autodl.exe` 放在 `C:/Tools/autodl/autodl.exe`。
可以把所在目录加入 PATH，也可以直接在插件里配置绝对路径。
只有将目录加入 PATH 后，才可以在任意位置直接运行 `autodl`。

PowerShell 中先做不访问账号或实例的本地检查：

```powershell
& 'C:/Tools/autodl/autodl.exe' --version
& 'C:/Tools/autodl/autodl.exe' --help
```

上游也支持 Go 1.26+ 源码安装：

```sh
go install github.com/saurlax/autodl-cli/cmd/autodl@latest
```

无需为了使用插件而安装 Go；使用发布包即可。
安装后，若更改了 PATH，重新启动 AstrBot 进程以继承新环境。

## 2. 选择凭据类型

先确认你使用的是哪类实例，二者的凭据不能混用：

| 实例类型 | 插件 `kind` | CLI 命令组 | 凭据 |
| --- | --- | --- | --- |
| Container Instance Pro | `pro` | `instance` | AutoDL 控制台“账号 → 设置 → 开发者令牌” |
| 普通容器 | `container` | `container` | 控制台网页登录令牌，属于上游实验性接口 |

普通容器的网页登录令牌可在本人已登录的 AutoDL 控制台中，通过浏览器开发者工具的
Application → Local Storage → `https://www.autodl.com` → `token` 获取，
或查看本人实例列表请求的 Authorization 请求头。请按密码对待该令牌。
不要把令牌发给 Bot、粘贴进 WebUI 配置或提交到 Git。

### 方式一：系统凭据存储

推荐本机交互使用。在 PowerShell 中执行相应命令，在隐藏输入提示中粘贴自己的令牌：

```powershell
# Pro 实例：保存开发者令牌。
& 'C:/Tools/autodl/autodl.exe' config set-token

# 普通容器：保存网页登录令牌。只需要执行与你的实例类型对应的一条。
& 'C:/Tools/autodl/autodl.exe' config set-web-token
```

Windows 下上游 CLI 默认保存到系统凭据管理器。
运行 AstrBot 的操作系统账号须能访问该凭据。
采用此方式时，插件配置中**省略 `token_env`**，不要填写不存在的环境变量名称。
仅保存令牌不会验证账号可用性。

### 方式二：环境变量引用

适合已有环境变量管理的场景。在启动 AstrBot 的同一个 PowerShell 会话中设置变量：

```powershell
$credential = Read-Host '输入你的 AutoDL 令牌' -AsSecureString
$env:MY_PRIVATE_AUTODL_TOKEN = [System.Net.NetworkCredential]::new('', $credential).Password
Set-Location 'F:/AstrBot'
uv run main.py
```

之后在插件表单“令牌环境变量名”中填写 `MY_PRIVATE_AUTODL_TOKEN`。
使用旧版 JSON 时对应 `"token_env": "MY_PRIVATE_AUTODL_TOKEN"`。
WebUI 里填的是**变量名**，不是令牌本身。
插件会把该变量的值仅传入 CLI 子进程，并按类型映射成 `AUTODL_TOKEN`
或 `AUTODL_WEB_TOKEN`。
已运行的 AstrBot 不会自动读到另一个新终端的变量；应在设置变量的环境中重新启动。
系统凭据存储和环境变量引用选择一种即可。

## 3. 手动查询已有实例（可选）

自动创建模式不需要你预先填写实例标识。本节用于核对已有资源或配置已有实例模式。

以下命令会访问 AutoDL 账号或已有实例，仅在你决定查询本人资源时自行执行：

```powershell
# Pro 实例：
& 'C:/Tools/autodl/autodl.exe' instance list
& 'C:/Tools/autodl/autodl.exe' instance status YOUR_INSTANCE_ID --json

# 普通容器：
& 'C:/Tools/autodl/autodl.exe' container list
& 'C:/Tools/autodl/autodl.exe' container status YOUR_INSTANCE_ID --json
```

选择与你的实例类型对应的命令组，将 `YOUR_INSTANCE_ID` 替换为已有实例的标识。
记录状态查询返回的实际运行中状态值，稍后用于配置 `running_status`。
示例中的环境变量 `MY_PRIVATE_AUTODL_TOKEN` 只由插件映射；手动运行 CLI 时，
如果不用系统凭据存储，需要自行设置相应的 `AUTODL_TOKEN` 或 `AUTODL_WEB_TOKEN`。

上游的 `instance get`、`container get` 和普通容器的原始 JSON 列表可能包含连接密码或
Jupyter 令牌，不要把这些输出粘贴进聊天、公开文档或 Git。
插件只保留脱敏的实例列表与状态。自动创建模式还会调用 Pro 的 `get`，
但只取连接主机与端口，不保存密码和其他连接令牌。

## 4. 配置插件

优先使用 WebUI 可视化表单，无需编写 JSON：

| 页面位置 | 选择或填写 |
| --- | --- |
| 配置来源 | 新用户保留“自动”；旧 JSON 用户填好表单后选“可视化表单” |
| 实验执行平台 | 下拉选择“AutoDL 云实例” |
| 本机 AutoDL CLI 路径 | 例如 `C:/Tools/autodl/autodl.exe`，不需要引号或参数数组 |
| 实例使用方式 | 按实验方案自动创建，或使用已有实例 |
| 实例类型 | 自动创建选 Pro；普通容器只能使用已有实例 |
| 令牌环境变量名 | 使用系统凭据时留空；环境变量方式只填变量名 |
| 资源档位 | 点击添加，分别填写名称、GPU 规格、镜像、CUDA 下限、数量、磁盘、地域 |
| 查询、创建、开机、关机权限 | 分别使用开关授权，默认全部关闭 |
| AutoDL 实验 SSH 连接 | 用户、密钥路径、专用根目录、Python、SSH 执行权限、主机密钥信任方式 |
| 已有实例 ID、SSH 地址及端口 | 仅已有模式手填；自动创建时由插件记录或获取 |
| 已有实例关机策略 | 下拉选择保持、归档后关机或总是尝试关机；自建实例固定自动关机 |

资源档位可以添加多个，模型根据实验需求从中选择并说明理由，
不会自动增加管理员未授权的规格。自动创建至少需要一档。
启用自动创建不会自动打开付费或远端权限；运行前需自行明确开启查询、创建、
关机和 SSH 执行权限，继续任务可能需要启动权限。
保存配置不访问 AutoDL，批准方案后才创建实例。

完整字段与分工设置见 [可视化配置教程](configuration.md)。

### 旧版 JSON 示例（可选）

继续使用高级 JSON 配置时，保留 README 中的 `participants`、`assignments` 等，
将其 `platform` 和 `platforms` 替换为下方内容。以下使用系统凭据存储，
因此没有 `token_env`。使用表单的用户不需要复制这些示例。

#### 按实验需求自动创建并关机

此示例启用了 Pro 创建、启动、查询、关机和 SSH 执行权限。
请先替换所有示例值，确认允许使用的 GPU、镜像和地域，再保存。
保存这些权限后，**批准实验方案即允许插件按方案创建付费实例**。
仅保存配置或讨论方案不会创建实例。

```json
{
  "platform": "my_autodl",
  "platforms": {
    "my_autodl": {
      "type": "autodl",
      "ssh": {
        "user": "root",
        "identity_file": "C:/Users/YOUR_USER/.ssh/id_ed25519",
        "root": "/root/dedicated-astrbot-experiments",
        "python": "python3",
        "allow_execute": true,
        "accept_new_host_keys": true
      },
      "autodl": {
        "command": ["C:/Tools/autodl/autodl.exe"],
        "mode": "create",
        "kind": "pro",
        "allow_create": true,
        "allow_discovery": true,
        "allow_start": true,
        "allow_stop": true,
        "startup_seconds": 300,
        "running_status": "running",
        "resource_profiles": {
          "small": {
            "description": "小规模对比实验，使用预装依赖和 SSH 公钥的私有镜像",
            "gpu_spec": "YOUR_APPROVED_GPU_SPEC_ID",
            "image": "YOUR_PREPARED_IMAGE_UUID",
            "cuda_min": 118,
            "gpus": 1,
            "disk_gb": 0,
            "region": ["YOUR_APPROVED_REGION"]
          },
          "large": {
            "description": "确有较大显存或多卡需求时才选择此档",
            "gpu_spec": "YOUR_LARGER_APPROVED_GPU_SPEC_ID",
            "image": "YOUR_PREPARED_IMAGE_UUID",
            "cuda_min": 118,
            "gpus": 2,
            "disk_gb": 0,
            "region": ["YOUR_APPROVED_REGION"]
          }
        }
      }
    }
  }
}
```

替换所有示例值，并按以下说明核对：

| 字段 | 填写说明 |
| --- | --- |
| `platform` | 填 `platforms` 中的条目名称，本例是 `my_autodl` |
| `command` | 本机 CLI 路径；如果 PATH 已配置，可用 `["autodl"]` |
| `mode` | `create` 为按需创建；`existing` 为使用已有实例，省略时兼容已有模式 |
| `kind` | 自动创建必须用 `pro`；已有模式可以用 `pro` 或 `container` |
| `instance` | 仅已有模式填写已有实例标识；创建模式由插件记录，不需要预填 |
| `resource_profiles` | 管理员允许的资源档位；方案只能选择其中一个，并说明理由 |
| `gpu_spec`、`image` | 有效的 GPU 规格标识和镜像 UUID，需要按你可用的资源填写，不能照抄占位符 |
| `cuda_min` | CUDA 驱动兼容下限，例如 `118` 表示 11.8；不是指定安装 CUDA |
| `gpus`、`disk_gb` | GPU 数量 1–4，系统盘额外扩容 0–500 GB |
| `region` | 允许使用的地域代码数组；空数组让平台按规格调度 |
| `allow_create` | 是否允许创建付费 Pro 实例，默认关闭 |
| `token_env` | 仅使用环境变量方式时添加；填写变量名，不填令牌 |
| `allow_discovery` | 是否允许查询列表、状态和脱敏连接详情；自动创建和任务准备均需要为 true |
| `allow_start` | 是否允许插件启动实例；允许查询不代表允许开机 |
| `start_on_prepare` | 仅已有模式使用；创建模式创建即开机，恢复已关机的原实例需 `allow_start` |
| `allow_stop` | 是否允许插件关机，与开机权限独立 |
| `running_status` | CLI 返回的运行中状态原值；`running` 只是示例，应按自己的实例返回核对 |
| `stop_policy` | 仅已有模式使用；创建模式固定自动关机，必须允许关机，不提供“创建后放任运行”的选项 |
| `startup_seconds` | 等待创建后的实例达到运行状态的期限，1–3600 秒，默认 300 秒 |
| `shutdown_seconds` | 后台等待关机或创建结果核对的期限，1–3600 秒，默认 120 秒；到期仍不明确时通知人工核对 |
| `stopped_statuses` | 平台关机状态原值列表，默认 `["shutdown","stopped"]`；确认命中后才标记已关机 |
| `ssh.host`、`ssh.port` | 已有模式手填；创建模式从新实例的脱敏连接详情获取 |
| `ssh.identity_file` | 本机 SSH 私钥文件路径，不是私钥内容 |
| `ssh.root` | 你已授权使用的远端专用实验根目录，使用绝对路径 |
| `ssh.allow_execute` | 是否允许上传、预检和运行已批准实验；创建模式必须为 true，以免创建无法使用的实例 |
| `ssh.accept_new_host_keys` | 默认 false；示例明确允许首次信任新实例的主机密钥，仍拒绝已知密钥变更；不启用时需预先信任实际连接地址 |

插件的 SSH 使用非交互密钥认证或 SSH agent，不支持让 Bot 代填密码。
新建实例使用的镜像需预先配置公钥登录和实验依赖。
本地私钥须与镜像中的公钥对应；插件不自动从返回的密码切换为密码认证。
用户 SSH 配置被禁用，因此主机、端口、用户名和密钥引用要在插件中明确填写。
远端需具备 POSIX shell、Python 3.10+ 和 SCP，不需要 harness。

#### 使用已有实例（JSON 方式）

不希望自动创建时，按 README 的已有实例示例设置 `mode: "existing"`、
`instance` 及 SSH 地址。实例由你提前开机时，可关闭 `allow_start`，
并设置 `start_on_prepare: false`。如果希望归档完成后自动关机，明确设置：

```json
{
  "mode": "existing",
  "allow_discovery": true,
  "allow_start": false,
  "allow_stop": true,
  "start_on_prepare": false,
  "stop_policy": "after_archive"
}
```

以上只是 `autodl` 对象中的模式与权限部分，仍需 CLI、实例标识和 SSH 等完整配置。

## 5. 开始使用与自动收尾

保存设置并重新加载插件，发送 `research new 你的研究想法`。
模型根据实验需求从 `resource_profiles` 中选择适用档位，
方案包含选择理由、确切规格和“创建后自动关机”的生命周期约定。
如果没有合适档位，需要修改配置或方案，不能擅自扩容或创建不在允许列表内的资源。

查看方案后，用 `research approve TASK_ID 版本号` 批准。
插件随后创建并等待实例启动，获取脱敏连接地址，通过 SSH 执行，
归档证据并关机。`research status TASK_ID` 显示实验进程与实例的独立状态。
用户用 `research stop TASK_ID` 停止任务时，也会尽力保存当前证据并关机。

任务暂停、失败或需要用户输入时，自动创建的实例同样进入关机流程。
继续任务时复用原实例，按需重新启动，不再创建第二个实例。
没有获得有效实验证据的任务会保留缺口，不因实例已关机就声称实验成功。

创建前持久化唯一名称和提交意图。创建响应丢失或 Bot 重启时，
按该名称分页查找原实例，不重复创建。查不到或出现多个匹配会保留不确定状态并提示人工核对。
关机也保存提交意图；请求结果不明时只查询状态，不盲目重复提交。
平台未确认关机时不会报告完整完成。此时请在控制台核对实例，并继续任务以重新查询状态。

关闭实例不等于释放或删除数据，存储等费用以平台规则为准。
若本机 Bot 长时间离线、凭据失效或网络不可达，插件无法保证及时关机，
应在 AutoDL 控制台核对；重启后会恢复核对流程。
插件没有独立的 `research autodl create/list/start/stop` 消息命令，
生命周期操作由任务流程触发。
任何流程都不能突破 README 中对组内共享服务器的固定限制。

## 验证范围与参考

本插件已完成 AutoDL 适配的离线协议与策略测试，包括创建、资源档位批准、
响应丢失与重启恢复、失败和取消收尾、脱敏连接详情和关机确认。
本次开发未使用真实 AutoDL 凭据或获准操作真实实例，
因此实例创建与查询、开关机、SSH 执行和计费行为仍有真实验证缺口。
本教程中的命令是供用户自行配置使用的说明，不代表开发过程已执行这些命令。

- [上游使用说明](https://github.com/saurlax/autodl-cli#readme)
- [上游认证说明](https://github.com/saurlax/autodl-cli/blob/main/docs/authentication.md)
