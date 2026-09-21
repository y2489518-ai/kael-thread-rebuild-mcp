# 贡献指南

欢迎提交缺陷报告、脱敏格式样本、适配文档和修复。

## 问题反馈

通过 [GitHub Issues](https://github.com/y2489518-ai/kael-thread-rebuild-mcp/issues) 提供版本、运行方式、预期与实际结果、复现步骤及必要状态字段。解析问题请使用虚构内容保留事件结构，不提交真实私人 transcript 或凭据。

## 本地检查

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

部分测试使用子进程或可能调用 tmux；使用独立开发环境，不要创建名为 kael-rebuild-selftest-DO-NOT-CREATE 的 session。测试通过不等于完成宿主恢复验收。

## 提交变更

说明问题、改变后的行为和验证方式。解析规则变更添加最小回归样本；文档调整检查命令、链接和实现一致性。不要提交本机配置、备份、凭据或真实对话。

区分预览、单元测试和端到端验收，保留贡献者署名，历史结论注明适用环境与日期。
