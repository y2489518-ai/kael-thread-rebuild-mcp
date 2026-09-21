# 首次部署验收

先完成[安装指南](docs/installation.md)，再在独立测试会话中演练。

## 准备与预览

- 记录 Claude Code 版本、运行方式、工作目录和实际启动参数。
- 备份测试项目 transcript、宿主设置及启动配置。
- 指定独立测试会话、project_dir 和 state_dir。
- 准备几轮虚构对话，包括连续输入、未回复消息和实际使用的聊天插件消息。
- 执行 doctor、dirty、plan，核对 source_session_id 和目标，确认 blocked_reason 为空。
- 按预算策略核对 selected_turns、source_turns 和 dropped_oldest_turns。这些计数只覆盖解析器识别的回合，还需人工抽查原文。
- startup_frozen 取决于 transcript 是否包含可识别启动项；false 不一定是故障。dirty 也不是宿主上下文读数。

## 切换与验收

1. 用户审阅并同意后，调用 thread_rebuild_request，confirmation 为 REBUILD。
2. 助手完成当前回复，再由 Stop hook 或 runner 触发 worker。
3. 查看 thread_rebuild_status 和 state_dir 下的 worker.log。
4. 核对新 session 身份，抽查预计保留的首、中、尾对话。
5. 从实际聊天入口发送新消息，确认接收和回复正常。
6. pointer 模式还要核对 runner 下一次调用使用了新指针。

activated 只代表控制器完成了自身检查，不能替代第 4–6 步。

## 取消与恢复

pending 请求可用 thread_rebuild_cancel 取消，confirmation 为 CANCEL。

已激活操作可用 thread_rebuild_rollback_request 登记回滚，confirmation 为 ROLLBACK；回滚仍需后续 Stop hook 或 runner 触发。

自动恢复失败、宿主无法继续回复或出现 session_conflict 时，保留 operation 证据，由操作者检查目标身份、备份路径和原启动命令，再手动恢复旧 session。不要对身份不明的目标反复激活。

完成演练后，再为主会话配置并重复预览与收发验收。
