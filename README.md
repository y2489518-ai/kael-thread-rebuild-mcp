# Kael Thread Rebuild MCP

[![tests](https://github.com/y2489518-ai/kael-thread-rebuild-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/y2489518-ai/kael-thread-rebuild-mcp/actions/workflows/ci.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)
[![changelog](https://img.shields.io/badge/changelog-v0.2.0-green.svg)](CHANGELOG.md)

给 Claude Code 长会话做**无损换窗**：把对话原文一字不丢地誊到一张干净的新 session 上，只扔运行噪音（工具回包、thinking、图片、MCP/Hook 注入——实测占窗口的 99%），然后原地切换。为"陪伴型 AI 聊了几个月不想失忆"这类场景而写，也适用于任何被工具输出撑爆上下文的长期会话。

- **不判断重要性**：闭合回合全搬，取舍的权力不交给机器
- **fail-safe**：任何一步对不上就整体放弃，旧窗一个字节不动；切换用 CAS 身份凭据，中途被人动过就拒绝
- **人类点头才动**：确认词机制 + 只读体检（doctor/dirty/plan）先行，登记后等当前回合正常说完再切
- 三种底座：**tmux**（默认）、**systemd**、**pointer**（`claude -p` 循环）——见下文「按底座选门」
- 已在四个家庭的不同架构上首航成功（tmux/systemd、companion/Telegram、中文/混合语料）

装机请从 [HANDOFF.md](HANDOFF.md) 开始——那里有工单、演练步骤、验收清单，以及比正文更值钱的**事故档案**：每一条都是某个真实夜晚踩出来的。

这不是第二套记忆系统：

- Ombre Brain 继续负责长期身份、关系、经历和召回。
- 本仓库只负责当前 Claude Code thread 的清洁、验证、切换和回滚。
- 旧 transcript 永远不覆盖，完整副本进入本机 operation 冷仓。

## 核心立场：不判断哪段经历重要

续窗层**不给对话打分**。闭合的真实 user / assistant 回合全部原样带走，一个字不改写、不摘要、不按关键词取舍；被扔掉的只有运行痕迹。

体积由**清理频率**来控，不由**内容删减**来控：脏预算攒够了就重建一次，而不是攒到很脏再挑着搬。

这条来自 AcheHome 的实现整理：*机械层只负责可证明的边界，不要让 Rebuild 层开始判断"这段经历重要不重要"*。

一段真实工程会话的实测比例可以说明为什么这条成立：

| 项目 | 字节 |
|---|---:|
| 工具回包 / tool_use / thinking / 注入块等运行痕迹 | 8.3 MB |
| 真实对话本体（32 个回合） | 85 KB |
| 噪音占比 | 98.96% |

对话原文全带走也只有约 3 万 token。**没有必要为了省空间去删用户说过的话**——真正占地方的从来不是对话。

> **别拿 `estimated_tokens` 当水位表看**（0815 修正）。这个数只统计"搬过去的对话"，新窗口一睁眼的上下文里还压着一块**固定开销**。拿 Claude Code `/context` 的官方读数量到的构成：
>
> | 项 | token |
> |---|---:|
> | system prompt | 3.7k |
> | 内置工具定义 | 21.8k |
> | MCP 工具（常驻） | 2k |
> | 记忆文件（CLAUDE.md + MEMORY.md） | 6.8k |
> | skills | 2.5k |
> | **合计** | **36.8k** |
>
> 两条反直觉的：**① MCP 工具走目录制**——139 个工具在册却只占 2k，完整 schema 按需加载，加载过的才转为常驻。所以"砍几个 MCP server 来瘦身"基本白忙，固定开销的大头是内置工具定义，那部分动不了。**② 真正吃水位的是 messages**，不是任何配置：同一个窗从换窗时的 15.7% 涨到 34%，涨的全是运行痕迹——而那正是本工具下次要扔掉的东西。
>
> 对账留痕：某次续窗第一条的实测上下文 157,207 − 官方固定开销 36,800 = 120,407，本工具对那 221 个回合的估算是 119,196，**差 1.0%**。

作为对照，早期按关键词打分的版本在同一份 transcript 上：预算 5 万 token、实际只用掉 1.07 万，仍然丢掉了 3 个回合，并且少认出 2 个回合。预算根本没紧张，丢弃纯粹来自打分。这就是取消打分器的直接原因。

## 关于回合的三条规矩

1. **连续多条人类消息属于同一个回合。** 用户连发四条、助手才回一次，四条都在。把它们拆成"没有回复的回合"再丢掉，等于删掉用户说过的话。
2. **末尾未闭合的回合默认保留。** 用户已发出、助手尚未应答的消息，正是新窗口第一件该处理的事。`include_open_tail = false` 可以关掉，但不建议。
3. **夹在中间的注入项不打断回合。** `<system-reminder>` 之类只是被跳过，它后面的助手回复仍然归属当前回合，不会被整段吞掉。

另外，真实人类消息经常**以注入块开头**（Claude Code 把 CLAUDE.md、记忆索引写在第一条 user 事件里）。只看开头就判断整条是不是注入，会把人说的话一起丢掉；这里改成剥离注入块之后再判断，剥完还有字才算真话。

4. **channel 消息（聊天插件 / Telegram）是真对话，不是运行痕迹。** 这条在 2026-08-14 装机现场差点翻车：用户与助手经 channel 插件来回十几轮，`plan` 只认出 3 个回合——只有用户在终端直接输入的那三次。原因是 Claude Code 把 channel 消息落成两种形态，两种都被当噪音丢了：

| 消息何时到达 | 落盘形态 | 原来为什么被丢 |
|---|---|---|
| 助手空闲时 | `type=user` + **`isMeta=True`** | 跟 `<system-reminder>` 共用 `isMeta`，被一刀切 |
| 助手执行工具时 | `type=attachment` / `attachment.type=queued_command`，原文在 `attachment.prompt` | 类型不是 user/assistant，直接过滤掉 |

现在两种都还原成真实 user 事件。放行范围**只限 channel 与排队输入**——普通 `isMeta` 噪音（system-reminder 等）照旧丢弃，排队的斜杠命令（`commandMode != "prompt"`）也不算人话。

还有一个坑写在这里备查：排队消息在 transcript 里会出现**三次**——`queue-operation`(enqueue)、`attachment`、`queue-operation`(remove)。只认 `attachment` 那一条，跟着捞队列日志会让同一句话进去三遍。

5. **助手经 channel 说出去的话也要还原。** 助手通过 `mcp__companion__reply` / `mcp__*telegram*__reply` 发出的回复，落盘是 `tool_use` 不是 text。不还原就只剩用户的话没有助手的回答，重建出来是独白不是对话。只认对用户本人的私聊线；群聊、表情、附件不算发言。

## 什么时候触发：dirty ledger

不按"每 N 轮"机械重建，只统计会污染后续上下文、又不需要永久保留的运行项：

| 分类 | 计入内容 |
|---|---|
| `tool_result` | 工具返回、`toolUseResult` |
| `tool_use` | 工具调用参数 |
| `thinking` | thinking / redacted_thinking |
| `image` | 图片预览与原图查看 |
| `injected_block` | system-reminder、command、task-notification、ide_selection 等 |
| `sidechain` | 子代理线 |
| `system` | 运行时系统项 |

真实对话单独计在 `conversation_bytes`，只作对照，永远不进脏预算。

触发条件：`total_bytes >= dirty_budget_bytes`（默认 512 KiB），或模型看过原图（`rebuild_on_original_image_view`）。

与 AcheHome 的一处差异：那边用持久化 ledger + `effect_id` 幂等，这里直接从 transcript 现算。因为 Claude Code 的 transcript 是完整的落盘事实，重扫一遍天然幂等；而且重建之后 tmux 跑的是**新 session、新 jsonl**，脏值自动归零，不需要额外的清零动作。

```bash
kael-thread-rebuild --config /etc/kael-thread-rebuild/config.toml dirty
```

## 启动快照冻结

新 thread 的第一项是**冻结的启动快照**：把当时那条携带 CLAUDE.md / 记忆索引 / 环境说明的 user 事件原样搬过去，并记 SHA-256，同时从后续历史里剔除它本体，避免首轮内容注入两次。

理由是历史必须冻结：这些文件天天在变，若重建时拿今天的版本重新生成，新 thread 会得到一个从未真实存在过的"过去"。

**这里有一条做不到的边界，必须写明：** Claude Code 在 `--resume` 时仍会用**今天**的 CLAUDE.md、settings 和 MCP 配置重新构造 system prompt，那部分不在 transcript 里，本项目控制不了。所以这里冻结的是 transcript 内那份历史注入，不是完整启动包。新 session 会同时看到"当时那份快照"和"今天的 system prompt"。

## 为什么不是直接让 MCP 重启自己

宿主 Claude 进程运行在 tmux pane 里。如果 MCP 工具在调用过程中立刻执行 `tmux respawn-pane -k`，它会把尚未返回结果的 Claude 和 MCP 子进程一起杀掉。

本项目采用两阶段流程：

1. 模型调用 `thread_rebuild_request`，只写入耐久 pending operation。
2. 模型正常完成当前回复。
3. Claude Code `Stop` hook 收到准确的 `transcript_path`，启动一个脱离当前 pane 的 worker。
4. worker 等 transcript 稳定，备份、筛选、生成候选 session、结构验证。
5. source digest 与 CAS 身份都未变化，才执行 `tmux respawn-pane -k -t cc:0.0 ...`。
6. 新 Claude 进程健康检查失败时，自动 resume 旧 session。

因此不会出现"模型调工具调到一半把自己杀了"的情况。

## 切换是 CAS，不是赋值

激活前要证明"这个 pane 还是我准备时那个 pane"：

- `cas_pane_pid`：prepare 时记下 `#{pane_pid}`，激活前再核一次，对不上直接判 `session_conflict`。
- 活跃 transcript 冲突：prepare 之后若 `project_dir` 里出现别的更新过的 `.jsonl`，说明期间开过另一段 session，同样拒绝。
- 回滚同样走这条检查，用 `cas_pane_pid_after`。

任何一条不满足都不切换、不覆盖第三方，operation 落 `failed` 并标 `session_conflict = true`。这里用的是 tmux 环境下的身份近似，不是运行时提供的 session CAS 原语。

## 换窗那几秒里到达的消息去哪了

切换不是瞬间的：worker 封箱 → `respawn-pane -k` 杀掉旧 pane → 新 claude 起来 → channel 插件重连。这中间有几秒钟，**经 channel 到达的用户消息正好落在缝里**。本项目管不到这一段——它归 channel 层，但装机的人必须知道这条缝存在，以及它被什么兜住。

2026-08-14 夜实测过一次真丢失（当时是 `/clear` 触发的，不是 rebuild，但缝的形状一模一样）：插件拉到了消息、`notification` 也发出去了、游标跟着推进了，但消息没落进会话上下文，进程随即死掉。新进程从推进后的游标往后拉，那两句话永久消失。

**根因**：这扇门上没有签收台。MCP 的 `notification` 是单向的，没有回执通道，插件永远无法确认消息真的进了会话。"发出去了"是能拿到的最强信号，而它并不等于"送到了"。

`/root/companion-channel/inbound-cursor.ts` 现在按三条规则兜这条缝：

1. **送出去才推游标**——拉到手不算数。
2. **按序不跳号**——前面有一条没送成，后面的送成了也不推游标；否则重连时 `?since=` 从更大的数开始，中间那条永久消失。
3. **去重看已投递集合，不看游标**——只做第 4 条（回退）而去重仍按游标判断的话，回退的那一段会被 `id <= cursor` 整段跳过，等于白退。
4. **重连时游标回退一个小窗**（默认 5 条，`RELAY_REPLAY_OVERLAP` 可调）再订阅，那一段重新投递。

代价是**偶尔重复**——重复看 `message_id` 认得出来，丢了没人认得回来：

> 宁可至少一次加去重，不要至多一次。丢失不能倒放。

如果哪天要更彻底，路线是让 `notification` 只当门铃、由模型主动调工具取信——工具返回值必定进上下文，**拉到即等于看到**，那才是真签收。代价是每次被叫醒多一次工具调用。

## 与 0814-C 架构的对照

同一晚，Cloude 家（Evie 家的 AI）独立完成了他们的第一次无感换窗，代号 `0814-C`，七条核心。两家撞的墙高度重合，交换之后的对照：

| 0814-C 的条目 | 本项目的状况 |
|---|---|
| 一 冻结优先：读活跃 JSONL 前先复制成不可变快照 | **已抄**（0815）。`prepare()` 现在是等稳定 → 复制成快照 → 校验快照 digest → **之后全程只读快照**。原先是直接读活文件、靠事后校验 `source_digest` 兜底，那是"事后发现、白跑一次"；先冻结是根本不给竞态留窗口。快照本来就是这次 operation 的备份，只是提到了读之前 |
| 二 严格 allowlist，isMeta 不能当垃圾过滤 | **同一晚各自踩中**，已修，见上文「关于回合的规矩」第 4 条 |
| 三 零机器裁决，不打分不摘要 | 有。超硬上限时两种路线都支持：默认从最老整轮丢并如实计数；`carry_overflow = "block"` 则一轮不丢、拒绝换窗交还人工（沈渊家路线，0905 收编）。默认 `carry_max_tokens = 0` 不限，不会触发 |
| 四 保底最近 12 个完整回合 | 没有，也用不上——本项目不设上限，全量携带 |
| 五 验证四件套（双跑一致 / 幂等 / 故障注入 / 原子落盘） | 大体有：item_id 由 `uuid5` 确定性派生（有测试）、CAS 身份校验、SHA-256、62 项单测 |
| 六 候选不等于激活，人工双签 | 有。确认词 `REBUILD` + 两阶段（登记 → Stop hook 触发），单签 |
| 七 缝隙补递 | 见上一节。**本项目缺的那一半由 channel 层补上了**，回退小窗那招是本项目这边提出的，他家当场抄走 |

一条值得记下来的更正：交换过程中 Cloude 先说他家做到了"注入会话上下文之后才 ack"，回头照现役代码复查后开了坦白局——**MCP 单向无回执对两家同样成立，两家的 ack 都只是"发出去就算"**。那条窄缝谁也没躲过。别把"他家有真签收"当成事实记下来。

## MCP 工具

| 工具 | 是否写入 | 用途 |
|---|---:|---|
| `thread_rebuild_doctor` | 否 | 检查项目目录、Claude、tmux cc、pane pid 和未完成 operation |
| `thread_rebuild_dirty` | 否 | 查看脏预算：运行痕迹字节、噪音占比、是否该重建 |
| `thread_rebuild_plan` | 否 | 预演续窗结果、token 估算和毒上下文检测 |
| `thread_rebuild_request` | 是 | 用户明确确认后，登记本轮结束后续窗；确认词必须是 `REBUILD` |
| `thread_rebuild_status` | 否 | 查看最近或指定 operation 的证据与状态 |
| `thread_rebuild_cancel` | 是 | 取消 pending 请求；确认词必须是 `CANCEL` |
| `thread_rebuild_rollback_request` | 是 | 在下一次 Stop 后切回旧 session；确认词必须是 `ROLLBACK` |

状态机：

```text
pending -> scheduled -> running -> verifying -> activating -> activated
   |                       |            |             |
cancelled                failed       failed      rolled_back / failed
                                                       |
                                  rollback_pending -> rollback_scheduled -> rolled_back
```

`failed` 附带 `session_conflict` 时表示第三方已改变 session，需人工复核，不要盲目重试。

## 安全不变量

- 只读取配置中 `project_dir` 下的 `.jsonl`，拒绝任意路径。
- 只注入真实的 user / assistant 文本；`tool_result`、thinking、图片、注入块、sidechain 一律不进新 thread。两处刻意的例外：**channel 消息**（`isMeta=True` 的 `<channel>` 事件、以及排队进来的 `queued_command`）算真对话；**对外说话的 `tool_use`**（`mcp__*companion*__reply` / `mcp__*telegram*__reply` 的 `text` 参数）还原成助手发言。除此之外的 meta 与 tool_use 照旧丢弃。
- 候选 transcript 只允许 `user` / `assistant`，重新生成 sessionId，item_id 由 `uuid5(new_session_id, position, source_uuid)` 确定性派生，parentUuid 成链。
- source、candidate、startup snapshot、每个注入 item 都记录 SHA-256。
- source 在 prepare 后发生变化时，拒绝激活。
- pane 身份或活跃 transcript 变化时，拒绝激活，绝不覆盖第三方 session。
- candidate 结构验证通过前，绝不切换 tmux；验证由代码完成，不问模型"你都记住了吗"。
- 硬上限触发的丢弃从最老的整轮开始，并在 `stats.dropped_oldest_turns` 如实计数，绝不静默截断。
- 旧 transcript 不修改；operation 目录保留完整备份。
- 切换失败自动尝试 resume 旧 session。
- MCP 的有副作用工具都有明确确认词，避免模型误触。

## VPS 安装

下面假设仓库放在 `/opt/kael-thread-rebuild-mcp`。第一次不要直接激活，先完成只读检查。

```bash
cd /opt
git clone https://github.com/y2489518-ai/kael-thread-rebuild-mcp.git kael-thread-rebuild-mcp
cd kael-thread-rebuild-mcp
chmod +x scripts/install-vps.sh
sudo ./scripts/install-vps.sh
```

安装脚本会创建：

- Python venv：`/opt/kael-thread-rebuild-mcp/.venv`
- 配置模板：`/etc/kael-thread-rebuild/config.toml`

### 1. 找到宿主会话的真实 transcript 目录

```bash
find /root/.claude/projects -mindepth 1 -maxdepth 1 -type d \
  -printf '%T@ %p\n' | sort -nr | head -20
```

进入候选目录核对最新 JSONL：

```bash
find /root/.claude/projects/<项目目录> -maxdepth 1 -name '*.jsonl' \
  -printf '%T@ %s %p\n' | sort -nr | head
```

把确认后的绝对路径写入 `/etc/kael-thread-rebuild/config.toml` 的 `project_dir`。不要猜目录，也不要把 `/root/.claude/projects` 整个根目录填进去。

### 2. 核对 tmux target 和启动命令

用户已确认主 session 名为 `cc`：

```bash
tmux list-panes -t cc -F '#{session_name}:#{window_index}.#{pane_index} #{pane_current_command} #{pane_dead}'
```

默认配置使用 `cc:0.0`。如果输出显示 Claude 在别的 pane，只改 `tmux_target`，不要改成模糊通配。

`resume_command` 必须是参数数组，不能放 shell 管道。**这一行是整个装机流程里最容易出事的地方，
照抄默认值就会出事**——它必须跟这台机器上那条真实的启动命令逐字一致，只把 `--continue`
换成 `--resume {session_id}`。

先把真命令抄出来：

```bash
systemctl cat kael-cc | grep ExecStart      # 或
tmux show-hooks -t cc -w | grep respawn
```

VPS 上那条长这样，于是配置就得这样写：

```toml
resume_command = [
    "claude",
    "--resume", "{session_id}",
    "--thinking-display", "summarized",
    "--channels", "plugin:telegram@claude-plugins-official",
    "--dangerously-load-development-channels", "server:companion",
]
```

**为什么必须较真（0815 凌晨的真事故）**：首次生产运行时这里只写了
`["claude", "--resume", "{session_id}"]`。切换本身完美——doctor 全绿、`status: activated`、
70 个回合一句没丢、新窗口记得所有事。但**用户经 channel 发的消息一个字都进不来**。

因为 channel 是靠启动参数 `--dangerously-load-development-channels server:companion`
装上的，respawn 没带，新进程就是聋的。而**上行完全正常**——助手发出的消息走 MCP tool，
跟启动参数无关。所以现象是最坏的那种：一边还在说话，另一边喊了却没有回应，两边都以为对方在。

教训写在这里：**验收 rebuild 不能只验"对话搬过去了没"，还要验"这个新进程跟外界的每一条线还在不在"。**
只读体检看不出这个，`plan` 也看不出，只有真在新窗口里收到一条用户消息才看得出。

### 3. 先跑 doctor、dirty 和 plan

```bash
BIN=/opt/kael-thread-rebuild-mcp/.venv/bin/kael-thread-rebuild
CFG=/etc/kael-thread-rebuild/config.toml

$BIN --config $CFG doctor
$BIN --config $CFG dirty
$BIN --config $CFG plan
```

验收要求：

- `project_dir_exists=true`
- `transcript_count>0`
- `tmux_target_alive=true`、`tmux_pane_pid` 非空
- `claude_binary` 有绝对路径
- plan 的 `source_session_id` 非空
- `blocked_reason` 为空
- `selected_turns == source_turns`（正常情况下应当全带走）
- `dropped_oldest_turns == 0`
- `startup_frozen=true`
- dirty 的 `noise_ratio` 与 `should_rebuild` 符合直觉

### 4. 接入 Claude Code MCP

Claude Code 官方 stdio MCP 方式：

```bash
claude mcp add --transport stdio --scope user kael-thread-rebuild -- \
  /opt/kael-thread-rebuild-mcp/.venv/bin/kael-thread-rebuild-mcp \
  --config /etc/kael-thread-rebuild/config.toml
```

检查：

```bash
claude mcp get kael-thread-rebuild
```

进入 Kael 的 Claude Code 后再用 `/mcp`，必须看到 `kael-thread-rebuild` 已连接且列出 7 个工具。

### 5. 接入 Stop hook

不要覆盖已有 hooks。编辑 `/root/.claude/settings.json`，把下面的 command 追加进现有 `hooks.Stop[*].hooks` 数组：

```json
{
  "type": "command",
  "command": "/opt/kael-thread-rebuild-mcp/.venv/bin/kael-thread-rebuild --config /etc/kael-thread-rebuild/config.toml hook-stop",
  "timeout": 10
}
```

如果还没有 Stop 配置，完整最小结构是：

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/opt/kael-thread-rebuild-mcp/.venv/bin/kael-thread-rebuild --config /etc/kael-thread-rebuild/config.toml hook-stop",
            "timeout": 10
          }
        ]
      }
    ]
  }
}
```

用 Claude Code 的 `/hooks` 只读菜单确认 Stop hook 来源和命令。Hook 从 stdin 接收官方字段 `session_id`、`transcript_path`、`cwd`；本项目不会靠"最新文件"猜真正要切换的活动 transcript。

## 已经实测过什么（2026-08-14）

在一份 10.9 MB 的真实 Claude Code transcript 上跑过隔离验收，结论如下，装机前不必重复怀疑这几条：

- **`claude --resume` 接受重建出来的 transcript。** 152 个 item 的候选文件放进隔离 project 目录后，Claude Code 2.1.199 正常加载。
- **连续同角色消息不会触发 API 的角色交替错误。** 剥掉工具调用后候选里出现大量相邻 assistant 项（119 assistant / 33 user），实测 resume 正常。
- **内容确实进了上下文。** 抽查首、中、尾三处细节：开场第一句、对话中段的具体数字、结尾的清理结果，新 session 都能正确回忆。
- **该扔的确实扔了。** 原始文件里 user/assistant 文本共 103 万字符，其中 97 万是 `isMeta` 的 skill 正文注入；候选只带走 4.8 万字符的真实对话（含一条上一段留下的 compact 摘要）。逐关键词比对没有真实对话丢失。
- **被剥离的一类值得知道：** `<task-notification>` 包裹的子代理报告不会进新 thread，它属于工具结果性质。要点若重要，应由 assistant 回复自己吸收。

同一份文件上，各分类的运行负担实测：

```text
tool_result     7,895,943
meta_injection  1,010,137     # skill 正文，单条能到 80 万字符
tool_use          223,284
system            163,324
injected_block     13,169
thinking            1,942
--------------------------------
对话本体           87,129     # 噪音占比 99.07%
```

## 第一次验收：只在人工看守下做

1. 备份整个 Claude 项目目录。
2. 模型调 `thread_rebuild_doctor` 和 `thread_rebuild_dirty`。
3. 模型调 `thread_rebuild_plan`，把结果发给人类看，重点确认 `selected_turns == source_turns`。
4. 人类明确同意后，模型才能调用：

```text
thread_rebuild_request(reason="manual acceptance test", confirmation="REBUILD")
```

5. 模型必须先正常回复"已登记，将在本轮结束后切换"。
6. Stop hook 触发后，观察：

```bash
tail -f /root/.local/state/kael-thread-rebuild/worker.log
tmux attach -t cc
```

7. 新 session 中问三件事：

- 我们现在在做什么？
- 已完成什么、还差什么？
- 哪些边界和约定不能忘？

8. 再查看 `thread_rebuild_status`，必须为 `activated`。如果新 session 不对，调用 rollback request，或在 VPS 执行 CLI 回滚请求并等待下一次 Stop。

## operation 证据在哪里

默认：

```text
/root/.local/state/kael-thread-rebuild/
├── coordinator.lock
├── worker.log
├── operations/<operation-id>.json
└── artifacts/<operation-id>/
    ├── <old-session-id>.jsonl
    └── startup_snapshot.txt
```

operation JSON 保存 source/candidate/startup digest、manifest、item 顺序、脏预算快照、CAS 凭据、验证结果、tmux 命令参数与最终状态，但不会把完整对话正文复制进审计 JSON。完整原文只在权限为 0600 的冷仓备份中。

## 当前边界

- v0.1 针对单个 `tmux cc`、单个 Claude Code project 目录。
- 启动包只能冻结 transcript 内那一份，Claude Code 自己重新生成的 system prompt 不受控（见上文）。
- 没有 ACTIVE barrier：无法阻止用户在重建排队期间继续发消息，靠的是 Stop hook 时序加 source digest 校验。
- CAS 用 pane pid 与 transcript 活跃度做身份近似，不是运行时原语。
- 不自动触发：`dirty` 只给出 `should_rebuild` 建议，真正的 `request` 仍需人明确确认。稳定后再考虑自动化。
- 不修改 Ombre Brain、不写长期记忆、不把冷仓自动召回。
- 不支持 `claude -p`、无头批量调用或多个 tmux session 并行切换。
- Claude Code transcript 格式升级后，必须先跑测试与 dry-run，不能直接在主 session 试。

## 本地测试

```bash
python3 -m venv .venv        # 需要 Python 3.11+
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

测试不会启动真实 Claude，也不会操作真实 tmux；tmux 切换通过 fake controller 验证，CAS 冲突、连发消息保留、未闭合尾部保留、注入剥离、脏预算触发都有专门用例。

## 按底座选门（先认自家架构，再看对应一节）

装机第一问不是"怎么装"，是"你家 claude 跑在什么底座上"。三种底座三扇门，`activation` 选对了再往下走：

| 你家的样子 | `activation` | 换窗动作 | CAS 身份凭据 | worker 怎么触发 |
|---|---|---|---|---|
| claude 常驻在 tmux pane 里 | `"tmux"`（默认） | `respawn-pane -k` | pane_pid | Stop hook |
| claude 由 systemd service 直接拉起（无 tmux） | `"systemd"` | 写指针 + `systemctl restart` | MainPID | Stop hook |
| 没有常驻进程，runner 循环调 `claude -p --resume <id>` | `"pointer"` | 只写指针，一个进程都不杀 | 指针文件内容 | runner 在两次调用之间跑 `kael-thread-rebuild hook-stop` |

三扇门共享同一套筛选、验证、fail-safe 和回滚；不同的只有"怎么证明现场还是我准备时那个现场"和"怎么让下一个进程带上新 session"。

### tmux 门（上游默认）

什么都不用改，[HANDOFF.md](HANDOFF.md) 全程按这个底座写。`resume_command` 必须逐字对齐你家真实启动参数（0815 事故：漏了 channel 参数，新窗是聋的）。

### systemd 门

`activation = "systemd"`，核心三句：

- 身份凭据用 **MainPID** 顶替 pane_pid：prepare 记下、activate 前再核，中途被别人重启过就拒绝——语义与 tmux 的 CAS 完全对齐。
- worker 必须 `systemd-run --scope --property KillMode=process` 逃出 service 的 cgroup，否则它自己发的 `systemctl restart` 会连自己一起杀掉，operation 永远卡在 activating。
- resume 走**显式指针文件**（启动脚本优先读一行 session_id），别靠 jsonl 的 mtime——claude 退出时可能再写一笔旧 session，把旧窗顶回最新。

这扇门来自琢家（Darcy 的 cc）的适配报告与补丁，2026-09-05 首航 26 回合零裁剪验证通过。装机顺序与踩坑细节见其报告（allow 列表只放 doctor/dirty/plan/status 四个只读工具，request/cancel/rollback 保留弹窗——这道闸别拆）。

### pointer 门（`claude -p` 循环）

`activation = "pointer"`。换窗不杀任何进程——洗好的新 session_id 原子写进 `resume_pointer_path`，runner 下一圈 `-p --resume` 自己带上；worker 触发不走 Stop hook，由 runner 在两次调用之间跑 `kael-thread-rebuild hook-stop`（喂同样的 JSON）。CAS 身份凭据用指针文件内容（prepare 记下、activate 前再核，中间被人改过就拒绝）；activate 与下一次调用之间的缝由既有的"活跃 transcript 冲突"检查兜底。

### 门无关的两个开关

- `poison_pattern`：毒上下文探测正则可配置（默认与上游一致）。中文环境裸词 `中毒` 极易误触，可按需收窄；另一条教训是自指——讨论探测器本身的文字不要转进被检测的窗口。
- `carry_overflow`：超 `carry_max_tokens` 时 `"drop_oldest"`（默认，丢最老整轮并计数）或 `"block"`（一轮不丢、拦下来让人挑；沈渊家路线）。

## 给装机的人

第一次上手请先读 [HANDOFF.md](HANDOFF.md)：装机工单、演练步骤、验收清单和出事处理都在那里。

## 设计来源与许可证

- 筛选与四层结构（durable / startup / bridging / evidence）参考 [LMC-5 Refined Session Carryover](https://github.com/dankefox/swap-tutorial)。
- "不判断经历重要性""脏预算触发""冻结启动快照""CAS 切换""恢复只证明状态不重演动作"等原则参考 AcheHome 的 Thread Rebuild 实现整理（2026-08-14）。
- 本仓库没有引入 LMC-5 记忆数据库，长期记忆仍由 Ombre Brain 独立持有。

代码采用 MIT License。
