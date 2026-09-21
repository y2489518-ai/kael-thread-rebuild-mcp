# 配置与对话预算

配置使用 TOML，通过 CLI 的 --config 指定。默认值以 [RebuildConfig](../src/kael_thread_rebuild/config.py) 为准。[示例配置](../examples/config.toml)使用占位路径，部署时必须替换。

## 两种预算

| 配置 | 默认值 | 含义 |
|---|---|---|
| dirty_budget_bytes | 524288 | 运行记录达到该体积时给出重建建议 |
| carry_max_tokens | 0 | 携带对话的估算预算，0 为不限 |
| carry_overflow | drop_oldest | 移除最老整轮；block 为拒绝超预算换窗 |
| max_event_chars | 0 | 单条文本截断阈值，0 为不截断 |
| include_open_tail | true | 保留末尾尚未回复的用户消息 |
| freeze_startup_snapshot | true | 存在可识别启动项时携带历史快照 |
| stamp_turns | true | 每轮首条用户消息添加时间 |
| rebuild_on_original_image_view | true | 原图查看可使 dirty 建议重建 |

dirty 不会单独登记换窗请求。携带预算与运行记录阈值独立。

## 60,000 token 示例

```toml
carry_max_tokens = 60000
carry_overflow = "drop_oldest"
max_event_chars = 0
include_open_tail = true
```

该策略保留最近整轮，不按重要性打分，也不提供任意勾选历史回合的界面。若希望超预算时交给操作者处理，将 carry_overflow 改为 block。

估算只覆盖计入的对话回合，不是新 session 的完整 token 总数。宿主 system prompt、工具定义及额外启动内容也占空间。默认不限预算并不意味着上下文无限。

drop_oldest 至少保留最后一轮；单轮过长仍可超预算。启动快照也未完整计入预算。字符估算会随语言和内容类型产生偏差，应预留余量并核对宿主实际读数。

## 运行方式

tmux 使用 tmux_target、claude_workdir 和 resume_command；后者须包含独立参数 {session_id}，并保留真实运行所需的 channel 等参数。

systemd 需显式配置 activation、systemd_unit、resume_pointer_path。服务名和指针路径必须与目标环境一致。启动脚本必须读指针；控制器检查主进程命令行是否含目标 session ID。

pointer 需配置 activation 和 resume_pointer_path。runner 必须在调用之间传入 hook-stop 所需字段，等待操作完成后再读取指针启动下一次调用。本项目不提供完整 runner。

## 其他检查

verify_workdir_matches_project 默认启用；当前实现按工作目录中的 / 替换为 - 检查项目目录名。宿主规则不同时应先验证适配。

poison_pattern 是近期文本的正则匹配规则，最近十轮命中至少两次会阻止换窗；可能因普通用词或讨论规则本身而误触，不是语义判断或安全保证。
