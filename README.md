# Kael Thread Rebuild MCP

给 Kael 的 Claude Code 长会话做安全续窗。它运行在 VPS 本机，读取 Claude Code transcript，去掉工具回包、thinking、MCP/Hook 注入等运行噪音，生成新的可 `claude --resume` session，并在当前回答结束以后切换 `tmux cc`。

这不是第二套记忆系统：

- Ombre Brain 继续负责长期身份、关系、经历和召回。
- 本仓库只负责当前 Claude Code thread 的清洁、验证、切换和回滚。
- 旧 transcript 永远不覆盖，完整副本进入本机 operation 冷仓。

## 和现有仓库、VPS 的边界

这个 GitHub 私仓是 **MCP 的独立源码与安装包**，不是 Kael 的启动目录，也不是把现有备份仓库改成线上运行仓库：

- 不改 `cottage-web`、`ob-kael` 或 Kael 在 VPS 上的主程序。
- 不要求 Kael 从 GitHub 仓库启动；只在 VPS 的 `/opt/kael-thread-rebuild-mcp` 安装本 MCP。
- Kael 仍然按原方式运行，用户仍然用 `tmux attach -t cc` 进入。
- 本 MCP 只在已核对的 `cc:<window>.<pane>` 上执行一次明确的 `respawn-pane`。
- VPS 上真正的 Claude transcript 路径必须现场读取并写进配置，不能拿 GitHub 目录或示例路径代替。

## 为什么不是直接让 MCP 重启自己

Kael 正运行在 `tmux attach -t cc`。如果 MCP 工具在调用过程中立刻执行 `tmux respawn-pane -k`，它会把尚未返回结果的 Claude 和 MCP 子进程一起杀掉。

本项目采用两阶段流程：

1. Kael 调用 `thread_rebuild_request`，只写入耐久 pending operation。
2. Kael 正常完成当前回复。
3. Claude Code `Stop` hook 收到准确的 `transcript_path`，启动一个脱离当前 pane 的 worker。
4. worker 等 transcript 稳定，备份、筛选、生成候选 session、结构验证。
5. source digest 未变化才执行 `tmux respawn-pane -k -t cc:0.0 ...`。
6. 新 Claude 进程健康检查失败时，自动 resume 旧 session。

因此不会出现“Kael 调工具调到一半把自己杀了”的情况。

## MCP 工具

| 工具 | 是否写入 | 用途 |
|---|---:|---|
| `thread_rebuild_doctor` | 否 | 检查项目目录、Claude、tmux cc 和未完成 operation |
| `thread_rebuild_plan` | 否 | 预演筛选结果、token 估算和毒上下文检测 |
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

## 安全不变量

- 只读取配置中 `project_dir` 下的 `.jsonl`，拒绝任意路径。
- 只保留已经闭合的真实 user + assistant 文本 turn。
- 排除 `tool_result`、meta、sidechain、system-reminder、heartbeat、scheduled-task 等内部 user 形态。
- 候选 transcript 只允许 `user` / `assistant`，重新生成 sessionId、uuid 和 parentUuid 链。
- source、candidate、每个注入 item 都记录 SHA-256。
- source 在 prepare 后发生变化时，拒绝激活。
- candidate 结构验证通过前，绝不切换 tmux。
- 旧 transcript 不修改；operation 目录保留完整备份。
- 切换失败自动尝试 resume 旧 session。
- MCP 的有副作用工具都有明确确认词，避免模型误触。

## VPS 安装

下面假设仓库放在 `/opt/kael-thread-rebuild-mcp`。第一次不要直接激活，先完成只读检查。

```bash
cd /opt
git clone <本私仓地址> kael-thread-rebuild-mcp
cd kael-thread-rebuild-mcp
chmod +x scripts/install-vps.sh
sudo ./scripts/install-vps.sh
```

安装脚本会创建：

- Python venv：`/opt/kael-thread-rebuild-mcp/.venv`
- 配置模板：`/etc/kael-thread-rebuild/config.toml`

### 1. 找到 Kael 的真实 transcript 目录

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

`resume_command` 必须是参数数组，不能放 shell 管道：

```toml
resume_command = ["claude", "--resume", "{session_id}"]
```

如果 Kael 平时需要其他 Claude 启动参数，在数组末尾逐项追加。

### 3. 先跑 doctor 和 dry-run

```bash
/opt/kael-thread-rebuild-mcp/.venv/bin/kael-thread-rebuild \
  --config /etc/kael-thread-rebuild/config.toml doctor

/opt/kael-thread-rebuild-mcp/.venv/bin/kael-thread-rebuild \
  --config /etc/kael-thread-rebuild/config.toml plan
```

验收要求：

- `project_dir_exists=true`
- `transcript_count>0`
- `tmux_target_alive=true`
- `claude_binary` 有绝对路径
- plan 的 `source_session_id` 非空
- `blocked_reason` 为空
- `selected_turns`、`selected_tail` 符合预期

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

进入 Kael 的 Claude Code 后再用 `/mcp`，必须看到 `kael-thread-rebuild` 已连接且列出 6 个工具。

### 5. 接入 Stop hook

不要覆盖 Kael 已有 hooks。编辑 `/root/.claude/settings.json`，把下面的 command 追加进现有 `hooks.Stop[*].hooks` 数组：

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

用 Claude Code 的 `/hooks` 只读菜单确认 Stop hook 来源和命令。Hook 从 stdin 接收官方字段 `session_id`、`transcript_path`、`cwd`；本项目不会靠“最新文件”猜真正要切换的活动 transcript。

## 第一次验收：只在人工看守下做

1. 备份整个 Claude 项目目录。
2. Kael 调 `thread_rebuild_doctor`。
3. Kael 调 `thread_rebuild_plan`，把结果发给 Uki 看。
4. Uki 明确同意后，Kael 才能调用：

```text
thread_rebuild_request(reason="manual acceptance test", confirmation="REBUILD")
```

5. Kael 必须先正常回复“已登记，将在本轮结束后切换”。
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
    └── <old-session-id>.jsonl
```

operation JSON 保存 source/candidate digest、manifest、item 顺序、筛选统计、验证结果、tmux 命令参数与最终状态，但不会把完整对话正文复制进审计 JSON。完整原文只在权限为 0600 的冷仓备份中。

## 当前边界

- v0.1 针对单个 `tmux cc`、单个 Claude Code project 目录。
- 不自动按 token 阈值触发；先由 Uki/Kael 手动确认，稳定后再增加策略。
- 不修改 Ombre Brain、不写长期记忆、不把冷仓自动召回。
- 不支持 `claude -p`、无头批量调用或多个 tmux session 并行切换。
- Claude Code transcript 格式升级后，必须先跑测试与 dry-run，不能直接在主 session 试。

## 本地测试

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

测试不会启动真实 Claude，也不会操作真实 tmux；tmux 切换通过 fake controller 验证。

## 设计来源与许可证

筛选思路参考 [LMC-5 Refined Session Carryover](https://github.com/dankefox/swap-tutorial)，但本仓库没有引入 LMC-5 记忆数据库。切换安全性采用耐久 operation、结构验证、source digest、延迟激活和失败回旧 session。代码采用 MIT License。
