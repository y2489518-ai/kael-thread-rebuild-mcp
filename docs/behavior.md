# 工作原理与已知限制

## 处理流程

Stop hook 或 runner 提供实际 transcript 路径。worker 等待文件短暂稳定，复制源文件并核对摘要，再从副本提取对话。候选获得新 session ID 和事件链，验证后检查源文件与目标身份，再切换。

operation 保存状态、摘要、事件清单和检查结果。完整旧 transcript 留在本机备份中。激活失败时尝试恢复旧 session；恢复也可能失败，需要操作者介入。

## 保留范围

解析器识别 user / assistant 文本、部分 channel 消息、queued_command 排队输入，以及名称匹配 companion / telegram reply 的工具调用文本。工具结果、thinking、图片、sidechain 和识别出的注入块不携带。

文本会规范化空白；时间标记会添加前缀，max_event_chars 可截断文本。这不是逐字节复制，也不保证模型行为与原窗口相同。

## 当前实现边界

以下行为未在本次文档整理中修复：

- 用户正文中与注入标签同名的内容也可能被删除，例如讨论 system-reminder 标签的示例。
- user 事件同时含文字与 tool_result 时会整条排除。
- 聊天 reply 提取不检查接收对象，也不以工具调用成功回执为前提；不能承诺只携带与某个人的成功送达消息。
- JSONL 无法解析的行会被跳过。
- manifest 在筛选清理后生成，校验能发现候选写入不一致，不能独立证明筛选阶段未漏掉原话。
- 启动快照仅对 transcript 中可识别的启动项生效，不冻结宿主重新生成的 system prompt。
- tmux 健康检查主要看 pane 存活；systemd 还检查主进程命令行中的 session ID；pointer 检查指针。均不能证明消息端到端收发成功。
- 文件摘要与进程或指针身份检查不是宿主原子会话锁。切换期间消息需要 channel / runner 协调、重放或去重。
- 宿主格式、插件结构或启动行为变化后，应先在独立测试会话验证。

公开 main 的 dirty 统计还会将 isMeta channel 事件计入 meta_injection；因此 dirty 分类不能直接等同于对话携带结果。

## 数据与效果

源 transcript 不被本项目覆盖；候选写入 project_dir，备份和日志写入 state_dir。完整备份包含私人对话，状态和启动参数可能含本机信息，公开排障前需要脱敏。

用自己的脱敏会话对比宿主上下文读数、预计保留范围和实际候选文本，并完成收发验收。具体节省比例取决于会话内容。编码任务清掉工具证据后可能需要重新读取文件或重跑检查。
