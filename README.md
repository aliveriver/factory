# Research Experiments for AstrBot

An independent plugin for minimal research comparisons. AstrBot routes
`research` commands directly to the plugin, which keeps task state, local harness
sessions, discussion, plan approval, execution evidence, and notifications.
Ordinary scheduling and status queries do not call a chat model.

This implementation covers design stages A-C. Real research tuning and the
stage D evaluations are deliberately separate.

## Installation

Install `https://github.com/aliveriver/factory` through AstrBot's plugin
management, or copy this repository's files to
`<AstrBot data>/plugins/astrbot_plugin_research` and reload plugins in AstrBot.
The plugin entry and metadata are at the repository root.

Use AstrBot's existing Python environment (`uv sync` at the repository root).
The plugin adds no Python dependencies beyond the host's `filelock`. The host
currently requires Python 3.12+, while plugin and remote supervisor syntax uses
Python 3.10+. Install and authenticate each harness locally yourself. No remote
harness is required or installed.

Configure `settings_json` in the plugin settings. Keep tokens, passwords, and
private key contents out of that JSON. SSH uses an identity **file reference**
or the SSH agent. AutoDL uses a named environment reference or its existing
system credential store.

Only AstrBot administrators and explicitly configured `allowed_users` may use
the plugin. Each allowed identity is the compact JSON encoding of
`[platform_id,sender_id]`, for example `["my-telegram","12345"]`. Task ownership
checks use both authenticated platform and sender identity, independently of
the conversation return address.

## Minimal settings

Replace every example model/command with an explicitly selected available one.
There is no default model or silent fallback.

```json
{
  "participants": {
    "builder": {
      "harness": "codex",
      "model": "YOUR_EXACT_CODEX_MODEL",
      "command": ["codex", "app-server"],
      "web_search": "live",
      "role": "Implementation and final synthesis"
    },
    "reviewer": {
      "harness": "deepseek",
      "model": "[\"YOUR_PROVIDER\",\"YOUR_MODEL\"]",
      "command": ["dsh", "--profile", "acp"],
      "role": "Research and fairness review"
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

On Windows, launch DSH through Node explicitly rather than relying on a
PowerShell or batch wrapper:

```json
["C:/path/to/node.exe", "C:/path/to/npm/node_modules/@deepseek-ai/dsh/lib/bin.js", "--profile", "acp"]
```

The verified first adapters target Codex app-server JSON-RPC and the official
`@deepseek-ai/dsh` ACP profile. DeepSeek's model selector is an **opaque ACP
value**, normally a JSON array encoded as a string. Copy a value from the
profile's live `configOptions`; do not assume that a display name is a selector.
Models are recorded separately as configured, discovered, and successfully
called. A catalog entry alone does not prove account access. Task `sessions`
retains the last discovery/call state; failed calls invalidate its verification.
The plugin does not issue repeated billable validation pings.

`agents_file` is optional. The plugin reads applicable ancestor `AGENTS.md`
files plus the explicitly selected user file. It never creates, overwrites,
or populates AGENTS with the task goal, assignments, or metrics. Every
participant receives the same text and file fingerprints before its turn.
Changes are reread before continuing; the task stores the applied versions.
Task details live separately in `task.json` and `experiment.md`.

## Commands

Prefix these with the Bot's configured command prefix, when required:

```text
research new Compare my proposed modification with the existing baseline...
research list
research status TASK_ID
research reply TASK_ID Answer to the current clarification
research supplement TASK_ID A change to the idea or supporting information
research approve TASK_ID 1
research reject TASK_ID 1
research stop TASK_ID
research continue TASK_ID
research artifacts TASK_ID
research plan TASK_ID
research models TASK_ID
research configure TASK_ID {"limits":{"repair_rounds":7}}
research configure TASK_ID {"assignments":{"summary":["reviewer"]}}
```

`configure` selects existing administrator-configured participants by name
(`{"participants":["builder","reviewer"]}`), and can override their stage
assignments and task limits. It cannot add an arbitrary launcher, model,
credential, filesystem root, or platform from a message. Settings changes require
inactive work; participant/assignment changes invalidate plan approval.

Each plan records baseline, modification, data/conditions, principal metric,
minimal scale, decision rule, resource policy, literal argv commands, and
required evidence. Approval binds the exact current version. No reply remains
a wait; an old reply cannot approve a replacement plan. The first release
always asks for plan approval, including clear ideas. This conservative default
does not require users to estimate token or compute budgets.

Questions return to the recorded task and participant session. Native questions
and permission requests are different event types. In this release native tool
execution escalations can be declined with `reply TASK_ID deny` or stopped;
`allow` is rejected. Experiment execution requires the separate approved plan
and the administrator's platform permission.

Core idea changes require inactive work and form a new plan. Completed,
rejected, or cancelled tasks remain archived; submit a new task to begin new
work. A pause preserves diagnosis and current counters. Raise an exhausted
limit with `configure`, then continue. No-improvement and insufficient-evidence
conclusions do not trigger tuning or additional experiments.

## Harness and execution boundaries

Harnesses run locally. Codex uses a read-only sandbox with shell, code mode,
apps, plugins, hooks, browser/computer use, MCP servers, and delegation disabled.
The plugin appends `dsh-research.patch.yml` after the configured ACP profile to
disable local shell, filesystem, workflow, goal, and subagent tools. Public web
search/fetch remains available through the installed harness's own web tools.
Search success is recorded through actual tool events; a configured switch does
not constitute a successful search.

Harnesses return implementation content through the structured `files` field.
The plugin applies only relative files inside the private task workspace,
rejects escaping paths/symlinks and AGENTS writes, and syntax-checks Python
before running. Multiple writers run serially. This restriction keeps platform
operations, file changes, and resource enforcement in the plugin.

`allow_execute` defaults to false. Enabling it authorizes running the explicitly
approved experiment code with the configured OS account. The local/SSH
supervisor is a timeout and process-control mechanism, **not a security sandbox
for hostile Python code**. Use an account and workspace appropriate for trusted
research code. Local experiment processes receive a small OS environment,
without inheriting model/API credential environment variables.

`model_calls` counts plugin-owned harness turns, including research,
discussion, coding, repairs, analysis, and synthesis. A harness may make internal
provider calls that its protocol does not expose. Actual, cached, estimated,
and unknown usage are separate; DSH ACP context occupancy is not billed usage.
The optional `tokens` value is an observed stopping threshold, not an exact
provider-side hard cap. Calls, rounds, task active time, and supervisor runtime
remain enforceable when token totals are unavailable.
After an unclean restart, elapsed wall time since the last active checkpoint is
charged conservatively and reported separately as `recovery_wall_seconds`;
restarting the Bot does not reset the task's time allowance.

## SSH platforms

For a server with explicit authorization for uploads and experiments:

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

SSH targets need a POSIX shell, Python 3.10+, and SCP support. Client transport
uses OpenSSH, noninteractive authentication, existing trusted host keys, and
legacy SCP with quoted POSIX paths. User SSH config is disabled; resolved
addresses are pinned. Each run gets a unique directory. Uploads exclude local
run data and refuse symlinks. Downloads have size ceilings and compare remote
and local SHA-256; logs and artifacts are preserved per run.

The supervisor is copied as plain Python; no harness or third-party dependency
is installed remotely. It writes stdout, stderr, process status and exit code,
enforces a deadline, and observes a cancellation file. SSH disconnection does
not mean process failure. The Bot records the submission intent first and
queries that original run after reconnect/restart. An uncertain launch pauses
rather than submitting again. Monitor loops can supervise an existing process
while model work is paused; they never submit new experiments.

**Shared server `10.8.176.151` is blocked from this executor**, including
destinations resolving to that address. Generic preflight, upload, run, query,
cancel, collect, repair, recovery and cleanup cannot access it. The separately
authorized one-time login/mkdir test is not an installed plugin feature.
The October 24, 2026 test used exactly:

```sh
ssh duankaidi@10.8.176.151 'mkdir -- "$HOME/astrbot-plugin-login-test-20261024"'
```

It exited 0 with empty stdout/stderr. This proves only login and mkdir command
success. The directory remains; no subsequent command or cleanup is authorized.
All other SSH acceptance is controlled/offline, not shared-server validation.

## AutoDL

Use the user-selected [saurlax/autodl-cli](https://github.com/saurlax/autodl-cli).
Instance lifecycle and SSH execution are separate:

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

`kind` is `pro` or `container`; ordinary container support is experimental in
the upstream CLI. They use separate developer/web credentials. Credential
availability does not grant lifecycle authorization. Discovery/status,
startup, and shutdown have separate permissions. Only existing instances are
supported; create/release are intentionally absent.

The adapter exposes sanitized list/status and explicit start/stop calls.
Task preparation checks instance status and optionally starts only when
`start_on_prepare` and `allow_start` are both enabled. Startup intent persists;
an ambiguous response is queried rather than blindly repeated. Configure the
SSH endpoint independently; the plugin does not request secret-bearing
`instance get` output.

`stop_policy` is `leave`, `after_archive`, or `always`. `always` attempts the
independently authorized stop even when archive collection fails;
`after_archive` leaves an incomplete archive's instance running and records that
state for intervention. Shutdown intent persists; an uncertain stop is queried
before any further action. Task status, process status, and instance status
remain separate, including when connection or shutdown fails.

No AutoDL CLI, instance credentials, or lifecycle authorization was provided
for this implementation's real validation. Contract tests use injected
transports only. Actual instance discovery, start/stop, SSH access and billing
behavior remain unverified.

## State, recovery, and artifacts

Runtime data uses `StarTools.get_data_dir("astrbot_plugin_research")`, which
delegates to this checkout's `astrbot.core.utils.astrbot_path`. This checkout
does not contain the older `path_utils` module named in the repository guidance.

```text
<plugin data>/<task UUID>/
  task.json                 # Atomic versioned record and notification outbox
  experiment.md             # Current separate experiment agreement and result
  transcripts/              # Raw stage output and constraint fingerprints
  workspace/                # Structured implementation files
    .runs/<run UUID>/        # Request, status, cancellation marker and raw logs
      source/               # Versioned local execution copy and its outputs
      archive/              # Immutable local evidence snapshots
      download/             # Downloaded SSH evidence
```

Records include original idea/supplements, owner and return address, versioned
approval history, assignments/session references, counters, submission IDs,
code/constraint fingerprints, reproduction argv, manifest and results.
Credentials stay in their external reference locations.

Restart pauses interrupted model work; it does not replay all history or assume
the experiment stopped. Existing runs are reconciled by ID. Supervisors keep
their deadlines independently of the Bot; background monitors retrieve terminal
evidence and apply the configured instance policy. `continue` resumes from the
recorded stage and the same harness references. Native request IDs expire when
a transport restarts, so reply to the resumed conversation rather than an old
permission request.

Missing or corrupt downloads remain `not_saved`; deliberate large remote-only
references are explicit in the manifest and do not masquerade as local archives.
Incomplete required evidence pauses final delivery. Continuing the archive
stage retries collection without rerunning experiments or model analysis.
Notification failure stays in the outbox; `status` and `artifacts` remain usable
even when proactive messages are unsupported. No files or remote directories are
automatically deleted.

## Verification

Use an AstrBot development checkout with its dependencies installed. From that
checkout's root, run the tests in this separate plugin clone:

```sh
uv run python -m pytest /absolute/path/to/factory/tests -q
```

Tests load this clone under the plugin's install name, independently of the
directory's name. They use the real AstrBot host imports from that development
environment. From the plugin repository root, check formatting and lint with
Ruff:

```sh
ruff format .
ruff check .
```

The offline tests explicitly label harness, RPC and SSH/AutoDL transport doubles.
They cover real entry loading, ownership, default-LLM bypass, durable states,
clarification/approval/rejection, two-participant handoffs, AGENTS preservation,
limits/repair, cancellation, recovery, archive errors, and instance policy.
Separate controlled tests run actual local Python supervisors, not SSH.

Real harness verification is opt-in and consumes model calls. Supply a private
settings JSON with your exact two real participants and assignments:

```sh
cd /absolute/path/to/AstrBot/data/plugins
/absolute/path/to/AstrBot/.venv/bin/python -m astrbot_plugin_research.verify_harnesses \
  --settings /private/path/research-settings.json \
  --output /private/path/local-harness-acceptance
```

On Windows, use the host environment's `.venv/Scripts/python.exe`. The module
command requires the installed directory to be named `astrbot_plugin_research`.

This verifies real clarification, both participants' discussion and synthesis,
explicit models, unchanged AGENTS fingerprints, persistent sessions,
cancellation/continuation and actual web-tool events. It saves local JSON and
transcripts. It fixes execution permission to false and never approves the
fixture plan. It does not contact SSH/AutoDL or measure a research improvement.

On October 24, 2026 the local real probes used
`codex-cli 0.163.0-alpha.10.2` and `@deepseek-ai/dsh 0.2.1-rc.2`.
Both real harnesses completed multi-turn input, cross-process session resume,
cancel, continued input, and actual search-tool calls. Codex and DeepSeek
exchanged design opinions and a designated Codex participant synthesized a
versioned plan that waited for approval. These are integration fixtures;
controlled local execution and offline SSH/AutoDL tests are separate evidence.
The implementation session tested Windows x86_64. Python 3.10 syntax was checked
for all plugin files; macOS, Linux, and Arm64 runtime execution still need their
own platform validation.

Protocol sources:

- [Codex app-server](https://developers.openai.com/codex/app-server), plus the
  installed binary's generated JSON schemas.
- [Official DeepSeek Harness ACP](https://github.com/deepseek-ai/deepseek-harness/tree/master/packages/acp/acp),
  plus the installed profile and package documentation.
- [AutoDL CLI](https://github.com/saurlax/autodl-cli), including the JSON envelope,
  command references, lifecycle and authentication source contracts.
