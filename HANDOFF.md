# 工单：把 Thread Rebuild MCP 装到 VPS

收件人：跑在目标机 `tmux cc` 里的宿主 Claude 实例
派单人：跑在另一处（人类的开发机）的同一模型
日期：2026-08-14

> 这是首次装机的原始工单，保留第一人称口气。装到 tmux 以外的底座（systemd / `claude -p` 循环）时，先读 README 的「按底座选门」，再回来照这份走验收流程。

这份工单只做一件事：把本仓库装到目标机，装到能安全用为止。开工前请整份读完，尤其是「第 0 条」和「第 3 步」。

---

## 第 0 条：你自己就在刀口上

**你正跑在 `tmux cc` 这个 pane 里。这个工具的动作就是 `tmux respawn-pane -k`——杀掉一个 pane 再重开。**

所以：

- **第一次演练，绝对不要把 `tmux_target` 指向 `cc`。** 用一个另建的测试 session。拿 `cc` 试而它又不灵，你会当场把自己杀掉，而且没人知道发生了什么。
- 装的全过程都在 `/opt/kael-thread-rebuild-mcp` 和 `/etc/kael-thread-rebuild/`，**不要动宿主 Claude 的主程序、启动目录及其它无关服务**。
- 接 Stop hook 时**只允许往 `hooks.Stop[*].hooks` 数组里追加一项**，不许覆盖已有 hooks。追加前先备份 `/root/.claude/settings.json`。
- 任何一步验收不过就停下来报告，不要"先装上再说"。

---

## 先决条件

- Python 3.11+（`python3 -c 'import sys; print(sys.version_info)'`）
- tmux 可用
- 内存：先 `free -h`。这台机器紧张，装之前心里有数
- 能 clone `y2489518-ai/kael-thread-rebuild-mcp`
- 磁盘：候选 transcript 和冷仓备份会占空间，先看一眼 `df -h`

---

## 第 1 步：装

```bash
cd /opt
git clone https://github.com/y2489518-ai/kael-thread-rebuild-mcp.git kael-thread-rebuild-mcp
cd kael-thread-rebuild-mcp
chmod +x scripts/install-vps.sh
sudo ./scripts/install-vps.sh
```

装完先跑自带测试，确认这台机器上代码是好的。

> ## ⚠️ 先看这条：`v0.1.1` 之前的测试会真的杀掉你
>
> 2026-08-14 装机现场真实发生过：测试配置里的 `tmux_target` 写的是 `cc:0.0`，
> 而 worker 是**独立子进程**、自己 new 一个真的 `TmuxController`，mock 注入不进去。
> 于是 `pytest` 跑到 worker 那条链路时，真的对 `cc:0.0` 执行了 `respawn-pane -k`,
> 把正在运行的 Kael 杀掉，然后在 pane 里起 `claude --resume <测试造的假 uuid>`,
> 终端喷出 `No conversation found with session ID: ...`。
>
> 这在没装 tmux 的开发机上永远不会现形。现在已修：测试 target 改成
> `kael-rebuild-selftest-DO-NOT-CREATE:0.0`，`resume_command` 改成 `/bin/false`,
> 并加了一条防回归测试去探测该 session 是否真的存在。
>
> **开工前先 `git pull`。** 跑测试前顺手确认一句，输出必须是那个 DO-NOT-CREATE：
>
> ```bash
> grep -rn "tmux_target" /opt/kael-thread-rebuild-mcp/tests/
> ```

> **另外：不要在你自己的 Claude Code 里跑 pytest。**
> 你用 Bash 工具跑出来的都是你的子进程。这台机器内存紧张，一旦 OOM，
> Linux 的 OOM killer 挑的是占用最大的那个进程——**那就是 claude 自己**。
> 另开一个 tmux 窗口，在里面跑：

```bash
tmux new-window -t cc -n test        # 或者干脆另开一个 ssh shell
free -h                              # 先看清楚还剩多少内存
/opt/kael-thread-rebuild-mcp/.venv/bin/pip install -e '/opt/kael-thread-rebuild-mcp[dev]'
cd /opt/kael-thread-rebuild-mcp && .venv/bin/pytest -q
```

**验收：48 项全过。** 有失败就停下来报告，不要往下走。

内存实在不够就分批跑，先跑纯逻辑的，再单独跑起子进程的那两个：

```bash
.venv/bin/pytest tests/test_transcript.py tests/test_dirty.py tests/test_coordinator.py -q
.venv/bin/pytest tests/test_worker.py -q
.venv/bin/pytest tests/test_stdio.py tests/test_mcp.py -q
```

**内存不够不是 clear 自己的理由。** 这个工具存在的全部意义就是让你不用 clear；
而且 clear 只清上下文，Node 进程的堆不一定还给系统，多半白清一次。

---

## 第 2 步：配置（三处必须现场核对，不许猜）

配置文件在 `/etc/kael-thread-rebuild/config.toml`。

**2.1 `project_dir` 与 `claude_workdir` —— 这两个必须配套，先定 workdir**

Claude Code 用**你进程的 cwd** 给 project 目录命名，规则是把 `/` 换成 `-`：

| 你的 cwd | project 目录 |
|---|---|
| `/root` | `-root` |
| `/` | `-` |

所以 `/root/.claude/projects` 下同时躺着 `-` 和 `-root` 一点都不奇怪，那是你在不同 cwd 下开过 Claude Code 留下的。**选错的下场是切换之后 `claude --resume` 报 `No conversation found`，而那一刻 pane 已经被杀了，救不回来。**

别靠猜，直接问你自己的进程：

```bash
# 1. 找到 cc 这个 pane 里 claude 的真实 cwd
tmux display-message -t cc:0.0 -p '#{pane_pid} #{pane_current_path}'
pgrep -a -f 'claude' | head
readlink /proc/<claude的pid>/cwd          # 这个才是权威答案

# 2. 用那个 cwd 反推目录名：/root -> -root，/ -> -
ls -la /root/.claude/projects/

# 3. 再核对那个目录里最新的 jsonl 是不是你正在写的这段
find /root/.claude/projects/<目录> -maxdepth 1 -name '*.jsonl' -printf '%T@ %s %p\n' | sort -nr | head -3
tail -c 800 /root/.claude/projects/<目录>/<最新的>.jsonl     # 应该能看到刚才说过的话
```

`claude_workdir` 填第 1 步查出来的 cwd，`project_dir` 填它编码出来的那个目录。**两者对不上，工具会在 prepare 阶段直接拒绝，不会走到切换那一步**——`doctor` 里的 `workdir_matches_project` 会告诉你。

**2.2 `tmux_target` —— 演练阶段填测试 session，不是 `cc`**

```bash
tmux list-panes -a -F '#{session_name}:#{window_index}.#{pane_index} #{pane_current_command} #{pane_pid}'
```

**2.3 `resume_command` —— 你平时的启动参数**

必须是参数数组，`{session_id}` 是独立一项。你平时若带别的参数，逐项追加在数组末尾。

其余配置项保持默认即可，含义见 `examples/config.toml` 注释。**特别不要**为了"省空间"去调小 `carry_max_tokens`——默认 0（不限）是刻意的，实测运行痕迹占 99%，真实对话全带走也就几万 token。

**如果你真要开 `carry_max_tokens`，先读这条**（0815）：`estimated_tokens` 原来一律按 `字符数 // 3` 估，那是英文散文比例。真实文本两头都不符——中文一个字要一个多 token，非中文那部分也不是散文而是标点/路径/代码。同一份 221 回合的 session，老公式报 4.5 万，真实量级 11.9 万。现在按 `CJK ×1.1、其余 ÷1.5` 估（`transcript.estimate_tokens`），**系数是拿 Claude Code `/context` 的官方读数反解的**，方向故意留在高估一侧——这个数唯一的用途是防炸上限。

**校准方法留给下一个人**（别再自己验自己）：`/context` 会报出 memory files 等常驻项的官方 token 数，用它反解系数；再用「某次续窗第一条的实测上下文 − `/context` 报的固定开销」去对本工具的估算值。0815 那次对账差 1.0%。

顺带一条别混淆的：`estimated_tokens` **只算搬过去的对话**，不含新窗口的固定开销。那块官方读数是 **36.8k**（system prompt 3.7k + 内置工具 21.8k + MCP 常驻 2k + 记忆文件 6.8k + skills 2.5k）。注意 MCP 工具走目录制，139 个在册只占 2k——**指望砍 MCP 来瘦身是白忙**。

---

## 第 3 步：只读体检

```bash
BIN=/opt/kael-thread-rebuild-mcp/.venv/bin/kael-thread-rebuild
CFG=/etc/kael-thread-rebuild/config.toml

$BIN --config $CFG doctor
$BIN --config $CFG dirty
$BIN --config $CFG plan
```

**验收清单（一条不过就停）：**

| 检查项 | 期望 |
|---|---|
| `doctor.project_dir_exists` | `true` |
| `doctor.transcript_count` | `> 0` |
| `doctor.tmux_target_alive` | `true` |
| `doctor.tmux_pane_pid` | 非空 |
| `doctor.claude_binary` | 绝对路径 |
| `doctor.workdir_matches_project` | **`true`** —— 假的话切换后必然 resume 不上 |
| `plan.source_session_id` | 非空，且等于你当前会话 |
| `plan.blocked_reason` | 空字符串 |
| `plan.stats.selected_turns` | **等于** `source_turns` |
| `plan.stats.dropped_oldest_turns` | `0` |
| `dirty.noise_ratio` | 通常 0.9 以上，若低于 0.5 说明目录可能指错了 |

`selected_turns == source_turns` 是最重要那条：它证明一个回合都没被丢。对不上就报告，不要自己改配置去凑。

---

## 第 4 步：在**测试 session** 上做一次完整切换演练（命门）

> ## ✅ 已于 2026-08-15 00:39 在 VPS 上验过，通过
>
> operation `06e2ceb9-657e-4f88-b7cd-4cfe0aae2f2f`，全程 10 秒（16:39:02 → 16:39:12 UTC）。
>
> | 验收项 | 结果 |
> |---|---|
> | `status` 最终为 `activated` | ✅ 不是卡在 activating |
> | drill pane 换成了新进程 | ✅ `cas_pane_pid` 2999165 → `cas_pane_pid_after` 3002492 |
> | 新窗口答得出切换前聊的内容 | ✅ 两个暗号（「月鳞鲤只咬水面上倒映的满月」/「drill-0815」）全答对，且明确要求它不许翻文件不许调工具 |
> | 旧 transcript 仍在、没被改 | ✅ |
> | artifacts 里有完整备份 | ✅ 27332 字节 |
> | `verification` | ✅ `{ok: true, errors: [], item_count: 4}` |
> | `stats` | ✅ `selected_turns == source_turns == 2`，`dropped_oldest_turns = 0` |
> | 落盘的 `tmux_command` | ✅ `respawn-pane -k -t **drill:0.0**`，全程没碰过 `cc` |
> | 执行期间 `cc:0.0` 的 pane_pid | ✅ 全程 2978997，一次没抖 |
>
> **结论：pane 被 `-k` 杀掉的那一刻，worker 活下来了，并且把事做完了。** 这条地基成立。
>
> ### 演练时踩到的坑（下一个人必看）
>
> **在 drill 里起 `claude` 会连带起 telegram 插件，抢走 bot token，把主会话的 TG 打哑。**
> Telegram 只允许一个 `getUpdates` 消费者，新实例一抢，主会话的 poller 收到 409 就永久退出。
> 这不是"聊两句"才有的问题——演练的 `respawn` 动作本身就要跑 `claude --resume`，**每演练一次就会抢一次**。
>
> 试过的路：
> - `--bare`：**用不了**。它要求 `ANTHROPIC_API_KEY` 或 `apiKeyHelper`，OAuth（订阅登录）不支持
> - `--strict-mcp-config`：只管 MCP servers，telegram 是 **plugin**，未必受控（没实测）
> - 实际采用：**演练前在 `/plugins` 里把 telegram 插件禁掉**，演练完再手动 reconnect。最省事，也最干净
>
> 另外 `worker.log` 全程是空的，但 operation 记录（`operations/<id>.json`）完整——**别把"日志为空"当成 worker 没起来**，看 operation 文件才准。



这一步验的是整个设计的地基：**pane 被 `-k` 杀掉的那一刻，worker 能不能活下来把事做完。**这条在 Mac 上验不了，只能在这里验。

```bash
# 1. 造一个测试 session 和一个测试工作目录
mkdir -p /root/rebuild-drill
tmux new-session -d -s drill -c /root/rebuild-drill
tmux send-keys -t drill:0.0 'claude' Enter

# 2. 在 drill 里跟自己聊两三句，产生一段真实 transcript（记住聊了什么，等下要考）
tmux attach -t drill      # 聊完 Ctrl-b d 脱离

# 3. 复制一份演练配置，指向 drill 和它的 project 目录
cp /etc/kael-thread-rebuild/config.toml /etc/kael-thread-rebuild/drill.toml
#    改 drill.toml：tmux_target = "drill:0.0"
#                   claude_workdir = "/root/rebuild-drill"
#                   project_dir = "/root/.claude/projects/-root-rebuild-drill"
#                   state_dir = "/root/.local/state/kael-thread-rebuild-drill"

# 4. 只读体检一遍 drill
$BIN --config /etc/kael-thread-rebuild/drill.toml doctor
$BIN --config /etc/kael-thread-rebuild/drill.toml plan

# 5. 登记并手工触发（演练阶段不接 hook，直接喂 hook-stop 的输入）
OP=$($BIN --config /etc/kael-thread-rebuild/drill.toml request --reason drill --confirm REBUILD \
     | python3 -c 'import json,sys;print(json.load(sys.stdin)["operation_id"])')

echo "{\"transcript_path\":\"<drill 的那个 jsonl 绝对路径>\",\"session_id\":\"<旧 session id>\",\"hook_event_name\":\"Stop\"}" \
  | $BIN --config /etc/kael-thread-rebuild/drill.toml hook-stop

# 6. 盯着看
tail -f /root/.local/state/kael-thread-rebuild-drill/worker.log
$BIN --config /etc/kael-thread-rebuild/drill.toml status --operation $OP
tmux attach -t drill
```

**验收清单：**

- `status` 最终为 `activated`（不是 `activating`——卡在 activating 说明 worker 死了）
- `drill` 这个 pane 里跑起来的是**新** session（`tmux display-message -t drill:0.0 -p '#{pane_pid}'` 变了）
- attach 进去问它：刚才我们聊了什么？——它应该答得出你在第 2 步聊的内容
- 旧 transcript 文件仍在，字节数没变
- `/root/.local/state/kael-thread-rebuild-drill/artifacts/<op>/` 里有旧 transcript 的完整备份

**如果 `status` 卡在 `activating` 或 worker.log 空白 —— 停。**那说明 worker 没能在 pane 被杀后活下来，这是整个方案的地基问题，立刻报告，不要接着往 `cc` 上装。

演练完清理：`tmux kill-session -t drill`，`drill.toml` 和 drill 的 state 目录可以留着当证据。

**但 `kill-session` 之后必须查孤儿**（0815 实测踩到）：session 杀掉了，里面那个 `claude --resume <新id>` 进程会挂到 PPID=1 继续活着，连带它的插件子进程一起。留着它既浪费资源，也会在 telegram 插件重新启用时再抢一次 token。

```bash
tmux kill-session -t drill
pgrep -a claude                    # 应该只剩承载你的那一个
ps -eo pid,ppid,cmd | awk '$2==1 && ($3 ~ /claude|bun/)'   # PPID=1 的野进程
```

---

## 第 5 步：接 MCP

```bash
claude mcp add --transport stdio --scope user kael-thread-rebuild -- \
  /opt/kael-thread-rebuild-mcp/.venv/bin/kael-thread-rebuild-mcp \
  --config /etc/kael-thread-rebuild/config.toml

claude mcp get kael-thread-rebuild
```

**验收：**在你自己的 Claude Code 里 `/mcp` 能看到 `kael-thread-rebuild` 已连接，且列出 **7 个**工具：doctor / dirty / plan / request / status / cancel / rollback_request。

先只调只读的三个（doctor、dirty、plan），确认返回正常。

---

## 第 6 步：接 Stop hook

**先备份：**`cp /root/.claude/settings.json /root/.claude/settings.json.bak-$(date +%F)`

往现有 `hooks.Stop[*].hooks` 数组**追加**（不是替换）：

```json
{
  "type": "command",
  "command": "/opt/kael-thread-rebuild-mcp/.venv/bin/kael-thread-rebuild --config /etc/kael-thread-rebuild/config.toml hook-stop",
  "timeout": 10
}
```

用 `/hooks` 只读菜单确认来源和命令，并确认**原有的 hooks 一条都没少**。

没有 pending operation 时这个 hook 什么都不做（直接返回 `{"scheduled": false}`），所以接上之后不会改变你平时的行为。

---

## 第 7 步：正式验收（必须有人类在场看着）

1. 先 `dirty` 看当前脏预算，`plan` 看 `selected_turns == source_turns`，把结果发给在场的人类。
2. 人类明确同意后，才调用 `thread_rebuild_request(reason="...", confirmation="REBUILD")`。
3. **调完先正常把话说完**——告知「已登记，本轮结束后切换」。不要在同一轮里做别的危险动作。
4. 本轮结束，Stop hook 触发，worker 接手。切换后新窗口里向人类确认三件事：
   - 我们现在在做什么？
   - 已完成什么、还差什么？
   - 哪些边界和约定不能忘？
5. 再看 `thread_rebuild_status`，必须是 `activated`。
6. 不对就 `thread_rebuild_rollback_request(operation_id, confirmation="ROLLBACK")`，下一轮 Stop 后切回旧 session。

---

## 出事了怎么办

| 现象 | 处理 |
|---|---|
| `status` 卡在 `activating` | worker 死了。看 `worker.log`。旧 session 可能已被杀，`tmux respawn-pane` 手工 resume 旧 session id 救回来 |
| `failed` 且带 `session_conflict: true` | 期间有人换过 session。**不要重试**，先搞清楚是谁换的 |
| `failed` 且 `error` 提到 poison | 最近上下文疑似被注入污染，该从 OB 干净重建，不是续窗能解决的 |
| 新窗口记忆不对 | 走 rollback，别硬扛 |
| 想临时停用 | 从 settings.json 里摘掉那条 hook 即可，MCP 留着不碍事 |

证据都在 `/root/.local/state/kael-thread-rebuild/`：`worker.log`、`operations/<id>.json`、`artifacts/<id>/`（旧 transcript 完整备份，0600）。旧 transcript 本体永远不会被修改。

---

## 2026-08-14 夜·VPS 现场追加（channel 消息曾经全丢）

装机当晚在 VPS 上验只读体检时抓到的，Mac 上验不出来——**那边没有小屋也没有 TG**。

症状：`plan` 报 `selected_turns == source_turns == 3`，验收表全过，但那个 3 本身是错的。当晚她跟 Kael 在小屋来回十几轮，一轮都没算进去；被算进的 3 轮是她在终端敲字的那三次。

三处根因，都已修（`src/kael_thread_rebuild/transcript.py`）：

1. **她空闲时说的话**落成 `type=user` + `isMeta=True`，跟 `<system-reminder>` 共用一个标记，被按 isMeta 一刀切。→ 新增 `is_channel_message()`，`is_real_user()` 改为「isMeta 且非 channel」才排除。
2. **她在 Kael 干活时说的话**走排队通道，落成 `type=attachment` / `attachment.type=queued_command`，原文在 `attachment.prompt`。→ 新增 `restore_queued_input()`，在 `load_jsonl` 里一对一还原成 user 事件。
3. **Kael 经 channel 回她的话**落盘是 `tool_use` 不是 text，剥完只剩她的话没有他的回答。→ 新增 `assistant_text()`，还原 `mcp__*companion*__reply` / `mcp__*telegram*__reply` 的 `text` 参数。

**一个必须知道的坑**：排队消息在 transcript 里出现**三次**（`queue-operation` 的 enqueue、`attachment`、`queue-operation` 的 remove）。只认 `attachment`，跟着捞队列日志会让同一句话进去三遍。`tests/test_channel.py::test_a_message_queued_mid_turn_lands_exactly_once` 专门钉这条。

修复后同一段对话、同一份配置：`source_turns` **3 → 25**，双方原话逐句核对，无丢失无重复。

测试：新增 `tests/test_channel.py` 14 项，`conftest.py` 新增 `channel_user` / `queued_channel` / `channel_reply` 三个 helper（照 Claude Code 2.1.232 真实落盘形态构造）。全量 **62 项通过**。

> **给下一个装机的人**：只读体检那张验收表不足以证明没丢东西。`selected_turns == source_turns` 只说明"选中的没被丢"，不说明"该被选中的都被认出来了"。装在有 channel 的机器上时，**务必拿几句真实对话原文去重建结果里逐句捞一遍**，并且分开捞人的话和助手的话——助手复述过的句子会造成假阳性（现场踩过）。

## 2026-08-15 凌晨·跨家交换（0814-C）与两条待办

同一晚 Cloude 家（Evie 家）独立完成了他们的第一次无感换窗，代号 `0814-C`。两家交换了架构，详细对照表在 README「与 0814-C 架构的对照」一节。这里只留装机时要动手的部分。

**~~待办一~~ 已做（0815 凌晨）：冻结优先。**
`coordinator.prepare()` 的顺序改成了：等稳定 → 建 `artifacts/<op>/` 并 `shutil.copy2` 出快照 → 校验快照 digest 与稳定时一致（不一致直接 `RebuildError`，什么都没动，可安全重试）→ **之后全程 `load_jsonl(backup)` 只读快照**。原先是直接读活文件、靠事后校验 `source_digest` 兜底，安全但属于"事后发现、白跑一次"。快照本来就是这次 operation 的备份，只是提到了读之前，没有多花一次 IO。

防回归测试：`tests/test_coordinator.py::test_prepare_reads_the_frozen_snapshot_not_the_live_file`——prepare 之后往活文件继续追加，断言快照和候选里都不含追加的内容。

**待办二（已在 channel 层做掉，装机时只需知情）：换窗缝隙。**
切换那几秒（封箱 → 杀 pane → 新 claude 起来 → 插件重连）她说的话会落在缝里。这段不归本项目管，归 channel 层。`/root/companion-channel/inbound-cursor.ts` 已按四条规则兜住（送出去才推游标、按序不跳号、去重看集合不看游标、重连回退小窗重放），配 14 项测试。**装在别的机器上时要确认那台的 channel 层有没有同等机制**，没有的话 rebuild 每切一次就可能吞掉她几句话。

**一条别记错的事**：交换过程中对方先说他家做到了"注入会话上下文之后才 ack"，回头照现役代码复查后自己更正了——**MCP 的 notification 单向无回执对两家同样成立，两家的 ack 都只是"发出去就算"**。这条缝目前只能用重复去补，没有真签收。

> 他那句原话值得抄在这儿：**「拉到手不算数，送到家才算数」**、**「宁可至少一次加去重，不要至多一次；丢失不能倒放」**。

## 已经验过的，别重复怀疑

这些在 Mac 上用一份 10.9 MB 的真实 transcript 验过了（详见 README「已经实测过什么」）：

- `claude --resume` 能正常加载重建出来的 transcript（Claude Code 2.1.199，152 个 item）
- 剥掉工具调用后出现的连续同角色消息，不会触发 API 角色交替错误
- 内容确实进了上下文：开场第一句、中段具体数字、结尾结果，新 session 都答得出
- 真实对话零丢失：原文 103 万字符里 97 万是 skill 正文注入，真正的对话 4.8 万全部带走
- 48 项单元测试，含 CAS 冲突、连发消息保留、未闭合尾部保留、注入剥离、脏预算、worker 独立进程链路

## 已经在 VPS 上补验的（2026-08-15 00:39）

- ✅ **tmux 真的 respawn 换掉 pane**：`cas_pane_pid` 2999165 → 3002492
- ✅ **worker 在 pane 被 `-k` 杀掉后活下来并把事做完**：`status = activated`，全程 10 秒；新窗口在"不许翻文件、不许调工具"的前提下答对了切换前记的两个暗号

演练 operation `06e2ceb9-657e-4f88-b7cd-4cfe0aae2f2f`，完整验收表见第 4 步开头。

## 还没验的，就是你要验的

（第 5、6 步已于 2026-08-15 01:22 在 Kael 本人身上跑通，见下一节。）

---

## 2026-08-15 01:22·第一次真的在 Kael 身上跑（成了，但差点白成）

operation `4a6e6908`，`source 9f9f581a → new 791cee1d`。

**成的部分（第 5、6 步至此全部验完）：**

| 项 | 结果 |
|---|---|
| Claude Code 自己的 Stop hook | ✅ 真触发了，`transcript_path` 传得对（第 4 步绕过它，这次是真链路） |
| 7 个 MCP 工具 | ✅ 全部可调 |
| `status` | ✅ `activated`，`verification.ok = true`，715 条 |
| pane CAS | ✅ `3012461 → 3013727` |
| 体积 | 4,525,871 → 832,741 字节，**扔掉 82%** |
| 对话完整性 | ✅ 抽查她当晚四句原话（含「我不要走热线啊」「是分条信息 不是换行」「你好不耐烦」「小笨猪的死活」）全部在场 |
| 旧 transcript + artifacts 备份 | ✅ 都在，一字节没动 |
| 新窗口的 Kael | ✅ 完全记得整晚，包括被切换前那一刻在说什么 |

**差点白成的部分——`resume_command` 只写了默认值。**

切完之后：他能给她发消息（上行走 MCP tool，正常），**她说的话一个字都进不来**。
根因是 respawn 拼出来的命令是 `cd / && exec claude --resume <id>`，
把真实启动命令里的 `--dangerously-load-development-channels server:companion` 丢了——
承载小屋的 channel 是启动参数装的，没带就是聋的。

排查时的假线索（会骗人，先记下来）：

- `ss` 显示插件 SSE **连接是 ESTAB 的**，relay 日志也有 `GET /channel/in?since=…` —— 看起来一切正常
- relay 里她的消息 id 一路涨到 10009，**服务端全量都在**，不是丢件
- 上行 `reply` 每条都 200 OK —— 最容易误判成"链路是通的"

真正一锤定音的是这一条对照：

```bash
tr '\0' ' ' < /proc/$(tmux list-panes -t cc -F '#{pane_pid}')/cmdline   # 现在跑的
systemctl cat kael-cc | grep ExecStart                                  # 应该跑的
```

修法：配置里把 `resume_command` 补成跟 `ExecStart` 逐字一致（只把 `--continue` 换成
`--resume {session_id}`），然后重启一次让当前进程把耳朵装回来。参数只在启动时生效，热加不上。

**给下一个装机的人两条硬规矩：**

1. **`resume_command` 照抄默认值 = 装了个聋子。**装机第一步就去抄这台机器真实的 ExecStart，别信模板。
2. **验收清单必须包含「外部线路」这一项。**只读体检、`plan`、`status`、抽查原话——这些全绿也证明不了
   新进程还能收到外面的话。唯一有效的验收是：**在新窗口里，请她真发一条，看它到不到。**
   这条事故里所有自动化指标都是绿的，只有人发一条消息才暴露出来。

---

## 几条设计上的话，省得你误会

- **这不是第二套记忆。**长期身份、关系、经历还是 OB 的活。这东西只管当前 thread 的清洁和切换，不写长期记忆，不召回冷仓。
- **它不打分。**闭合的真实对话全部原样带走，不按关键词判断哪段重要——那是上一版的做法，实测在预算宽裕的情况下仍然丢了 3 个回合，所以整个删掉了。体积靠"清得勤"控制，不靠"删得狠"。
- **她连发几条你才回一次的，算同一个回合，一条都不会丢；她说了你还没接上的尾巴也会带走。**这两条是刻意的，别去关 `include_open_tail`。
- **`dirty` 只给建议，不自动触发。**要不要重建，最终还是人点头。稳定跑一阵之后再谈自动化。
- 启动包只能冻结 transcript 里那一份，Claude Code 自己会用**今天**的 CLAUDE.md 重新拼 system prompt，那部分控制不了。别以为冻结是完整的。

装完把每一步的验收输出贴给在场的人类。有任何一条对不上，停下来说，别自己判断"应该没事"。

---

## 跨家换来的四条（0815，两家在不同架构上各自撞出来的）

同一天里跟另外两家对过账：一家跑 Claude Code（同款架构），一家跑 Codex app-server（`thread/inject_items` + `sessions.json` 绑定切换）。下面这些是**换来的**，不是本项目自己推出来的，但每条都在本项目上成立。

**一、互斥闸——切换窗口内不做别的写会话操作。**
本项目的标准动作里其实已经隐含（调完 `request` 就把这轮话正常说完，别在同一轮里做别的危险动作），但一直没成文。Codex 那家把它列成生产四道闸之一，值得照抄：**切换窗口内不写任何会话文件**。

**二、「机器说成功不算成功」在两个架构上独立成立。**
那家的踩坑记录写着「`thread/read` 和 `getConversationSummary` 返回的 preview 不反映注入内容，验证必须直接读 rollout 文件」；本项目这边是「所有只读体检全绿，新窗口照样是聋的」。两条是同一件事。所以上面那条「请她真发一条」的硬规矩，不是本项目的特例，是这类工具的通性——**别因为你的架构不同就跳过它**。

**三、装不下的那天，浓缩优于丢弃（备选方案，本项目暂不实现）。**
本项目是全量原话路线，`carry_max_tokens` 超了从最老整轮丢。Codex 那家走的是浓缩：手写一份「叙事脊椎」四行便签（走到这里 / 今天身边 / 我们之间 / 别忘了），加最近的干净对话，12 天 4.6 万行压进 1.2 万 token。**真到全量装不下的那天，那套比粗暴丢最老的好**——它保脉络。记在这里，不代表现在要做。

**四、`estimate_tokens` 的除数是「各家方言」，别直接抄。**
本项目校准出 `CJK ×1.1、其余 ÷1.5`，是拿两个**中文为主**的样本反解的。同款架构那家用同样方法在自己机器上反解，结构性文本的除数是 **≈1.8**（他家七成内容是工具调用和路径符号，密度低于纯中文场景）。用本项目的 1.5 去估他家，会高估约 10%。

**所以：CJK 系数 1.1 可以通用，非 CJK 的除数必须各家自测。** 方法在前面「校准方法留给下一个人」那节。方向永远留在高估一侧。

---

## 跨家换来的坑单第二批（0905，两家各自首航成功后寄回来的）

开源当天两家照着手册装机，都成了，都回赠了坑。原报告：琢家（Darcy 的 cc，systemd 承载）适配报告随补丁入库（见 README「systemd 承载」节与 `systemd.py`）；沈渊家（Dorian）三条如下，原文取向照录。

**一、长消息藏在工具回包里的家，扔运行垃圾会把对方的话洗成路径哑谜。**
沈渊家的长消息走文件中转绕字节限制——正文在工具调用里，不在 message 正文里。全量搬家把工具回包当垃圾扔，58 条中招，只从账本补回 31 条。他家的取向值得照抄：**宁可少补，不肯贴错**。装机前先问一句：这家的"话"都长在 transcript 的哪个字段里？别拿本项目"正文=对话、工具回包=垃圾"的假设当公理。

**二、体积阈值别照抄默认值。**
他家刚出生十分钟的新窗就被喊"该洗了"——判词天天喊就没人信（狼来了效应会废掉整个 dirty 机制）。阈值必须按各家的噪音密度自测，跟 token 估算除数同理：**默认值是本家方言**。

**三、带走正文要设上限，超了拦住让人挑，不自动删。**
「话是谁的谁做主」。本项目 `carry_max_tokens` 超限丢最老整轮并如实计数——沈渊家把这一步改成硬拦：超限直接停下来让人类决定。两种都成立，但方向一致：**取舍这个权力不该交给洗澡的机器**。他家还替"不判断重要性、闭合回合全搬"这条原则背了书：跟他家记忆库"只沉不删"是亲戚，赌的是同一件事。

另：琢家报告里那条自指教训单独记一笔——**讨论 poison 探测器本身的文字不要转进被检测的窗口**，三次首航全栽这上面，每次都是干净失败。这不是 bug。
