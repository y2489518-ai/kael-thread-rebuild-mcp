# Kael Thread Rebuild MCP

[![tests](https://github.com/y2489518-ai/kael-thread-rebuild-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/y2489518-ai/kael-thread-rebuild-mcp/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**为 Claude Code 长会话重建上下文：按预算携带近期对话，移除工具运行记录，在新 session 中继续。**

Kael Thread Rebuild MCP 提供对话筛选、换窗预览、候选验证、会话切换和回滚请求。保留范围由用户配置；不使用模型对对话打分或生成摘要。

这是独立的社区开源项目，非 Anthropic 官方产品。

[安装指南](docs/installation.md) · [配置与预算](docs/configuration.md) · [工作原理与限制](docs/behavior.md) · [首次验收](HANDOFF.md) · [版本说明](CHANGELOG.md)

## 适用场景

- 在 Claude Code 中进行长期聊天、角色创作或连续讨论，希望保留近期对话的具体措辞。
- 会话积累了大量工具结果、图片和运行记录，需要为后续交流腾出上下文。
- 使用 tmux、systemd 或自行维护的 claude -p runner，并能够配置 MCP 和启动方式。

本项目操作本机 Claude Code transcript，不适用于 ChatGPT 或 Claude 网页聊天。它不提供长期记忆检索，可以与独立记忆系统配合使用。

## 主要能力

- **可配置携带预算**：按估算 token 数保留最近整轮；超预算可移除最老整轮，或停止并交由用户处理。
- **对话提取**：处理连续用户消息、未回复的末尾消息，以及已适配的 channel 和排队输入格式。
- **换窗预览**：doctor、dirty、plan 分别检查配置、运行记录体积和预计携带结果。
- **两阶段切换**：先登记请求，再在当前回复结束后由 worker 处理。
- **备份与验证**：保存源文件副本，校验候选内容、事件链及源文件变化，并检查切换目标身份。

## 按需设置对话预算

例如，携带最近约 60,000 token 的对话：

```toml
carry_max_tokens = 60000
carry_overflow = "drop_oldest"
max_event_chars = 0
include_open_tail = true
```

这是可选配置，代码默认 carry_max_tokens = 0，表示不限携带预算。drop_oldest 从最老整轮开始移除；block 则拒绝超预算的换窗。退出新窗口的旧回合仍保留在源 transcript 和 operation 备份中，不会自动召回。

若携带对话约 60,000 token、启动上下文约 30,000 token，新窗口初始占用约 90,000 token。对可用容量为 1,000,000 token 的会话，约为 9%。这是计算示例，不是固定开销或效果保证。

预算基于文本估算，不是严格的总上下文上限。drop_oldest 至少保留最后一轮，极长单轮仍可能超预算。详见[配置说明](docs/configuration.md)。

## 开始使用

需要 Python 3.11+、可运行的 Claude Code，以及受支持的运行方式。首次建议选择独立测试会话。

```bash
git clone https://github.com/y2489518-ai/kael-thread-rebuild-mcp.git
cd kael-thread-rebuild-mcp
python3 -m venv .venv
.venv/bin/pip install .
```

安装 Python 包后，仍需配置 transcript 路径、启动参数和 MCP/worker 接入。请继续阅读[安装指南](docs/installation.md)。

| 运行环境 | activation | 切换动作 | 接入要求 |
|---|---|---|---|
| tmux 中的常驻 Claude | tmux | 重建指定 pane 中的进程 | MCP + Stop hook |
| systemd 管理的服务 | systemd | 写指针并重启服务 | 启动脚本读取指针，worker 脱离服务 cgroup |
| 自行维护的 claude -p runner | pointer | 更新会话指针 | runner 在调用之间触发 worker 并协调下一次启动 |

## 使用流程

1. 检查配置并预览：doctor → dirty → plan。
2. 用户确认保留范围后，登记换窗请求。
3. 当前回复结束，worker 备份源 transcript、筛选对话并验证候选。
4. 检查源文件和目标身份后切换；激活失败时尝试恢复旧 session。
5. 检查 operation 状态，并在新窗口验证对话与消息收发。

## 使用边界

当前版本按规则提取文本，会规范化空白并剥离识别到的注入块，**不承诺逐字节无损或完整运行状态迁移**。用户正文中的同名标签、混合事件和未适配插件存在保留边界；聊天回复提取尚未按接收对象过滤。

工具结果可能包含代码任务的重要证据，移除后可能需要重新读取。进程存活不等于聊天通道恢复；首次使用及宿主升级后应完成[端到端验收](HANDOFF.md)。详见[工作原理与限制](docs/behavior.md)。

## 开发与贡献

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

测试不能替代真实宿主上的恢复验收。请阅读[贡献指南](CONTRIBUTING.md)，反馈问题时不要附带未脱敏的对话和凭据。

## 许可证

采用 [MIT License](LICENSE)。
