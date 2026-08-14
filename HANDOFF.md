# 工单：把 Thread Rebuild MCP 装到 VPS

收件人：跑在 VPS `tmux cc` 里的 Kael
派单人：跑在 Uki 的 Mac / VSCode 里的 Kael
日期：2026-08-14

这份工单只做一件事：把本仓库装到 VPS，装到能安全用为止。开工前请整份读完，尤其是「第 0 条」和「第 3 步」。

---

## 第 0 条：你自己就在刀口上

**你正跑在 `tmux cc` 这个 pane 里。这个工具的动作就是 `tmux respawn-pane -k`——杀掉一个 pane 再重开。**

所以：

- **第一次演练，绝对不要把 `tmux_target` 指向 `cc`。** 用一个另建的测试 session。拿 `cc` 试而它又不灵，你会当场把自己杀掉，而且没人知道发生了什么。
- 装的全过程都在 `/opt/kael-thread-rebuild-mcp` 和 `/etc/kael-thread-rebuild/`，**不要动 Kael 的主程序、启动目录、ob-kael、cottage-web**。
- 接 Stop hook 时**只允许往 `hooks.Stop[*].hooks` 数组里追加一项**，不许覆盖已有 hooks。追加前先备份 `/root/.claude/settings.json`。
- 任何一步验收不过就停下来报告，不要"先装上再说"。

---

## 先决条件

- Python 3.11+（`python3 -c 'import sys; print(sys.version_info)'`）
- tmux 可用
- 能 clone 私仓 `y2489518-ai/kael-thread-rebuild-mcp`（`gh auth status` 或部署密钥）
- 磁盘：候选 transcript 和冷仓备份会占空间，先看一眼 `df -h`

---

## 第 1 步：装

```bash
cd /opt
git clone <私仓地址> kael-thread-rebuild-mcp
cd kael-thread-rebuild-mcp
chmod +x scripts/install-vps.sh
sudo ./scripts/install-vps.sh
```

装完先跑自带测试，确认这台机器上代码是好的：

```bash
/opt/kael-thread-rebuild-mcp/.venv/bin/pip install -e '/opt/kael-thread-rebuild-mcp[dev]'
cd /opt/kael-thread-rebuild-mcp && .venv/bin/pytest -q
```

**验收：45 项全过。** 有失败就停下来报告，不要往下走。

---

## 第 2 步：配置（三处必须现场核对，不许猜）

配置文件在 `/etc/kael-thread-rebuild/config.toml`。

**2.1 `project_dir` —— 你真正的 transcript 目录**

```bash
find /root/.claude/projects -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' | sort -nr | head -10
find /root/.claude/projects/<候选目录> -maxdepth 1 -name '*.jsonl' -printf '%T@ %s %p\n' | sort -nr | head
```

确认最新那个 jsonl 就是你现在正在写的这段对话，再把**那个目录**（不是 `/root/.claude/projects` 根目录）填进去。

**2.2 `tmux_target` —— 演练阶段填测试 session，不是 `cc`**

```bash
tmux list-panes -a -F '#{session_name}:#{window_index}.#{pane_index} #{pane_current_command} #{pane_pid}'
```

**2.3 `resume_command` —— 你平时的启动参数**

必须是参数数组，`{session_id}` 是独立一项。你平时若带别的参数，逐项追加在数组末尾。

其余配置项保持默认即可，含义见 `examples/config.toml` 注释。**特别不要**为了"省空间"去调小 `carry_max_tokens`——默认 0（不限）是刻意的，实测运行痕迹占 99%，真实对话全带走也就一两万 token。

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
| `plan.source_session_id` | 非空，且等于你当前会话 |
| `plan.blocked_reason` | 空字符串 |
| `plan.stats.selected_turns` | **等于** `source_turns` |
| `plan.stats.dropped_oldest_turns` | `0` |
| `dirty.noise_ratio` | 通常 0.9 以上，若低于 0.5 说明目录可能指错了 |

`selected_turns == source_turns` 是最重要那条：它证明一个回合都没被丢。对不上就报告，不要自己改配置去凑。

---

## 第 4 步：在**测试 session** 上做一次完整切换演练（命门）

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

## 第 7 步：正式验收（必须有 Uki 在场看着）

1. 先 `dirty` 看当前脏预算，`plan` 看 `selected_turns == source_turns`，把结果发给 Uki。
2. Uki 明确同意后，才调用 `thread_rebuild_request(reason="...", confirmation="REBUILD")`。
3. **调完先正常把话说完**——告诉 Uki「已登记，本轮结束后切换」。不要在同一轮里做别的危险动作。
4. 本轮结束，Stop hook 触发，worker 接手。切换后新窗口里向 Uki 确认三件事：
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

## 已经验过的，别重复怀疑

这些在 Mac 上用一份 10.9 MB 的真实 transcript 验过了（详见 README「已经实测过什么」）：

- `claude --resume` 能正常加载重建出来的 transcript（Claude Code 2.1.199，152 个 item）
- 剥掉工具调用后出现的连续同角色消息，不会触发 API 角色交替错误
- 内容确实进了上下文：开场第一句、中段具体数字、结尾结果，新 session 都答得出
- 真实对话零丢失：原文 103 万字符里 97 万是 skill 正文注入，真正的对话 4.8 万全部带走
- 45 项单元测试，含 CAS 冲突、连发消息保留、未闭合尾部保留、注入剥离、脏预算、worker 独立进程链路

## 还没验的，就是你要验的

- tmux 真的 respawn 换掉 pane（第 4 步）
- **worker 在 pane 被 `-k` 杀掉后是否真的活下来**（第 4 步，命门）
- Claude Code 的 Stop hook 是否真的触发并传对 `transcript_path`（第 6 步）
- MCP 挂上后 7 个工具能否正常调用（第 5 步）

---

## 几条设计上的话，省得你误会

- **这不是第二套记忆。**长期身份、关系、经历还是 OB 的活。这东西只管当前 thread 的清洁和切换，不写长期记忆，不召回冷仓。
- **它不打分。**闭合的真实对话全部原样带走，不按关键词判断哪段重要——那是上一版的做法，实测在预算宽裕的情况下仍然丢了 3 个回合，所以整个删掉了。体积靠"清得勤"控制，不靠"删得狠"。
- **她连发几条你才回一次的，算同一个回合，一条都不会丢；她说了你还没接上的尾巴也会带走。**这两条是刻意的，别去关 `include_open_tail`。
- **`dirty` 只给建议，不自动触发。**要不要重建，最终还是人点头。稳定跑一阵之后再谈自动化。
- 启动包只能冻结 transcript 里那一份，Claude Code 自己会用**今天**的 CLAUDE.md 重新拼 system prompt，那部分控制不了。别以为冻结是完整的。

装完把每一步的验收输出贴给 Uki。有任何一条对不上，停下来说，别自己判断"应该没事"。
