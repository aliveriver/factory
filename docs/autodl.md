# AutoDL 安装、配置与使用

插件使用 `saurlax/autodl-cli` 管理已有实例，通过 OpenSSH 上传和运行实验。
这两条连接独立配置：**AutoDL CLI 管理实例，SSH 连接实例里的实验环境**。
插件不会自动安装 CLI，不会创建或释放实例，也不会在远端安装 harness。

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

之后在插件配置中填写 `"token_env": "MY_PRIVATE_AUTODL_TOKEN"`。
WebUI 里填的是**变量名**，不是令牌本身。
插件会把该变量的值仅传入 CLI 子进程，并按类型映射成 `AUTODL_TOKEN`
或 `AUTODL_WEB_TOKEN`。
已运行的 AstrBot 不会自动读到另一个新终端的变量；应在设置变量的环境中重新启动。
系统凭据存储和环境变量引用选择一种即可。

## 3. 手动查询已有实例

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
插件仅保留脱敏的实例列表和状态，不请求 `get`。

## 4. 配置插件

保留 README 中的 `participants`、`assignments` 等设置，
将其 `platform` 和 `platforms` 替换为下方内容。这里使用系统凭据存储，
因此没有 `token_env`：

```json
{
  "platform": "my_autodl",
  "platforms": {
    "my_autodl": {
      "type": "autodl",
      "ssh": {
        "host": "YOUR_INSTANCE_SSH_HOST",
        "user": "root",
        "port": 22222,
        "identity_file": "C:/Users/YOUR_USER/.ssh/id_ed25519",
        "root": "/root/dedicated-astrbot-experiments",
        "python": "python3",
        "allow_execute": false
      },
      "autodl": {
        "command": ["C:/Tools/autodl/autodl.exe"],
        "kind": "pro",
        "instance": "YOUR_EXISTING_INSTANCE_ID",
        "allow_discovery": true,
        "allow_start": false,
        "allow_stop": false,
        "start_on_prepare": false,
        "running_status": "running",
        "stop_policy": "leave"
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
| `kind` | Pro 用 `pro`，普通容器用 `container` |
| `instance` | 已有实例的真实标识，不是显示名称，也不是 SSH 主机地址 |
| `token_env` | 仅使用环境变量方式时添加；填写变量名，不填令牌 |
| `allow_discovery` | 是否允许查询列表与状态；AutoDL 任务准备需要查询状态，因此实际使用时必须为 true |
| `allow_start` | 是否允许插件启动实例；允许查询不代表允许开机 |
| `start_on_prepare` | 是否在准备阶段自动启动；必须同时启用 `allow_start` 才能执行启动 |
| `allow_stop` | 是否允许插件关机，与开机权限独立 |
| `running_status` | CLI 返回的运行中状态原值；`running` 只是示例，应按自己的实例返回核对 |
| `stop_policy` | `leave` 保持原状；`after_archive` 成功归档后尝试关机；`always` 即使归档失败也尝试关机，后两者需 `allow_stop: true` |
| `ssh.host`、`ssh.port` | 实例实际提供的 SSH 地址与端口，与 CLI 的实例标识无关 |
| `ssh.identity_file` | 本机 SSH 私钥文件路径，不是私钥内容 |
| `ssh.root` | 你已授权使用的远端专用实验根目录，使用绝对路径 |
| `ssh.allow_execute` | 是否允许上传、预检和运行已批准实验；首次配置先保留 false |

插件的 SSH 使用非交互密钥认证或 SSH agent，不支持让 Bot 代填密码。
需要预先配置可用的公钥登录和受信任主机密钥。
用户 SSH 配置被禁用，因此主机、端口、用户名和密钥引用要在插件中明确填写。
远端需具备 POSIX shell、Python 3.10+ 和 SCP，不需要 harness。

## 5. 开始使用与授权开关

保存设置并重新加载插件，先发送 `research new 你的研究想法`，检查澄清和方案讨论。
`ssh.allow_execute: false` 时可以讨论，但不能执行实验。
插件目前没有独立的 `research autodl list/start/stop` 消息命令；
实例管理在任务准备和收尾流程中按配置触发。

准备实际执行时，确认 SSH 和实验目录可用，再将 `ssh.allow_execute` 改为 true。
如果实例由你提前手动开机，保留：

```json
{
  "allow_discovery": true,
  "allow_start": false,
  "allow_stop": false,
  "start_on_prepare": false,
  "stop_policy": "leave"
}
```

以上是 `autodl` 对象中的权限部分。已有实例必须实际处于可连接状态。
当你明确希望插件自动开机、在成功归档后关机时，可以改为：

```json
{
  "allow_discovery": true,
  "allow_start": true,
  "allow_stop": true,
  "start_on_prepare": true,
  "stop_policy": "after_archive"
}
```

开启开机权限可能产生平台费用。请先确认实例与平台计费规则。
只有当前版本的实验方案获批后，任务才进入平台准备阶段。
普通 SSH 或 AutoDL 的任何通用流程都不能突破 README 中对组内共享服务器的固定限制。

## 验证范围与参考

本插件已完成 AutoDL 适配的离线协议与策略测试。
本次开发未使用真实 AutoDL 凭据或获准操作真实实例，
因此实例查询、开关机、SSH 执行和计费行为仍有真实验证缺口。
本教程中的命令是供用户自行配置使用的说明，不代表开发过程已执行这些命令。

- [上游使用说明](https://github.com/saurlax/autodl-cli#readme)
- [上游认证说明](https://github.com/saurlax/autodl-cli/blob/main/docs/authentication.md)
