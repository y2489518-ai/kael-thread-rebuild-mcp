# Changelog

## v0.2.0 — 2026-09-05

开源第一天就长成了社区版本。

- **systemd 承载**（琢家 / Darcy 的 cc 贡献）：`activation = "systemd"`，MainPID 当 CAS 身份凭据，worker 用 `systemd-run --scope` 逃出 service cgroup，resume 走显式指针文件。含 9 个新测试与适配报告结论。
- **pointer 承载**（回应 `claude -p` 循环家的反馈）：`activation = "pointer"`，换窗只写指针、不杀进程，worker 由 runner 在两次调用之间触发。
- **`carry_overflow = "block"`**（沈渊家 / Dorian 路线）：超 `carry_max_tokens` 时一轮不丢、拒绝换窗交还人工——"话是谁的谁做主"。默认仍为 `drop_oldest`。
- **`poison_pattern` 可配置**：默认与上游一致；中文环境可收窄避免"中毒"裸词与自指误触。
- README 重构为「按底座选门」；HANDOFF 收录第二批跨家事故档案（琢家、沈渊家）。

## v0.1.0 — 2026-08-15

首个可用版本：全量原话筛选、脏预算触发、冻结启动快照、两阶段 CAS 切换、回滚、7 个 MCP 工具、Stop hook worker。0815 凌晨在 Kael 本机首航成功。
