# 安装与接入

本指南以 Linux + tmux 为起点。需要 Python 3.11+、可运行且已登录配置的 Claude Code、tmux，以及已产生对话的独立测试会话。

## 安装

```bash
git clone https://github.com/y2489518-ai/kael-thread-rebuild-mcp.git
cd kael-thread-rebuild-mcp
python3 -m venv .venv
.venv/bin/pip install .
```

后续命令从仓库目录执行。MCP SDK 依赖为 mcp>=2,<3，请使用本项目虚拟环境。

## 本机配置

在仓库根目录创建 config.toml。下面路径和 target 仅为示例，必须替换为实测值：

```toml
project_dir = "/home/alex/.claude/projects/-home-alex-rebuild-drill"
state_dir = "/home/alex/.local/state/kael-thread-rebuild-drill"
claude_workdir = "/home/alex/rebuild-drill"
activation = "tmux"
tmux_target = "rebuild-drill:0.0"
resume_command = ["claude", "--resume", "{session_id}"]

carry_max_tokens = 60000
carry_overflow = "drop_oldest"
max_event_chars = 0
include_open_tail = true
```

project_dir 是测试会话实际使用的项目目录，不能填整个 projects 根目录。通过实际 transcript 和工作目录核对，当前代码会检查目录命名对应关系。

```bash
tmux list-panes -a -F '#{session_name}:#{window_index}.#{pane_index} #{pane_current_path} #{pane_pid}'
```

resume_command 应保留实际 channel 等启动参数；最小示例不会自动恢复额外聊天入口。

## 预览

```bash
.venv/bin/kael-thread-rebuild --config config.toml doctor
.venv/bin/kael-thread-rebuild --config config.toml dirty
.venv/bin/kael-thread-rebuild --config config.toml plan
```

dirty 和 plan 默认选择项目中最新的非待激活候选 transcript，可追加 --transcript 和真实绝对路径指定文件。核对 source_session_id、blocked_reason、selected_turns、dropped_oldest_turns 和 estimated_tokens。预算允许裁剪时，有旧回合退出可以是预期结果，见[配置说明](configuration.md)。

## MCP 接入

先将下面 /absolute/path/kael-thread-rebuild-mcp 替换为实际仓库绝对路径：

```bash
claude mcp add --transport stdio --scope user kael-thread-rebuild -- \
  /absolute/path/kael-thread-rebuild-mcp/.venv/bin/kael-thread-rebuild-mcp \
  --config /absolute/path/kael-thread-rebuild-mcp/config.toml
claude mcp get kael-thread-rebuild
```

在宿主中确认 MCP 连接和工具可用。

| 工具 | 用途 |
|---|---|
| thread_rebuild_doctor | 检查配置与运行目标 |
| thread_rebuild_dirty | 查看运行记录体积 |
| thread_rebuild_plan | 预览携带范围 |
| thread_rebuild_request | 登记换窗，确认词 REBUILD |
| thread_rebuild_status | 查看操作状态 |
| thread_rebuild_cancel | 取消 pending，确认词 CANCEL |
| thread_rebuild_rollback_request | 登记回滚，确认词 ROLLBACK |

## Stop hook

先备份宿主 settings.json，再合并下面配置。已有 Stop hooks 时仅追加 command 项，保留其他设置。路径同样替换为真实绝对路径。

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/absolute/path/kael-thread-rebuild-mcp/.venv/bin/kael-thread-rebuild --config /absolute/path/kael-thread-rebuild-mcp/config.toml hook-stop",
            "timeout": 10
          }
        ]
      }
    ]
  }
}
```

hook-stop 从 stdin 读取含 session_id、transcript_path、cwd 的 JSON。在宿主确认 hook 生效后，完成[首次验收](../HANDOFF.md)。

## systemd 与 pointer

systemd 需专门的启动脚本读取 resume_pointer_path，并使主进程命令行包含目标 session ID。只设置 activation 不会自动改造已有服务。worker 需能通过 systemd-run 脱离被重启服务的 cgroup，权限和服务结构需现场验证。

pointer 需自有 runner 在调用之间向 hook-stop 传入相同 JSON。worker 异步执行，runner 应等待操作完成后再读指针，并协调期间消息。它不是无需接入的通用无头运行器。

配置字段见[配置说明](configuration.md)。

## VPS 系统目录安装

scripts/install-vps.sh 默认使用 /opt/kael-thread-rebuild-mcp 和 /etc/kael-thread-rebuild/config.toml。脚本创建虚拟环境并复制配置模板，不会自动发现配置或接入 MCP/hooks。模板中的部署路径、tmux target 和 channel 参数必须逐项核对。
