# 有证据的后续复盘

`followup.py` 保存明确登记的调用，默认没有后台监听。记录位于用户私有运行目录，不能放进公开 Skill。

初始化：

```text
python3 <skill>/scripts/followup.py init --record <记录.json> --skill <已生效技能目录> --expectation <原验收标准> --scope <真正能记录到的调用范围>
```

默认七天后到期，至少三个同版本、可比较的实际使用结果再做常规判断；单个有证据的失败可立即调查。`--due` 可接受带时区的 ISO 时间，必须符合实际安排；受控时间测试要标为模拟，不能冒充经过一周的收益。

每次确实执行后登记：

```text
python3 <skill>/scripts/followup.py record --record <记录.json> --evidence <产物或检查文件> --result pass --kind actual --invocation <这次调用方式及任务标识> --runtime <实际环境>
```

开发用合成案例、隔离回放用 `--kind replay`，不会计入真实使用数量。相同证据、版本和调用标识不重复计数。原始私密输入不用复制，保存必要的结果证据即可。

`check --record <记录.json>` 只读返回：

- `not_due`：未到期，不能据此称效果达标。
- `unobserved`：记录尚未接通，不能推断没有人用。
- `insufficient_data`：已记录范围的可比较实际样本不足，保持版本。
- `needs_review`：到期有足够记录，或有明确失败；代理还要阅读证据、比较原标准和定位原因，脚本不替代语义判断。
- `version_changed` / `evidence_changed`：记录与现在不一致，先核对，不能继续沿用旧通过结论。

复盘完成可用 `review --record <记录.json> --conclusion <结论与依据> --evidence <复盘正文>` 追加，保留当时自动检查状态。缺数据时结论明确“数据不足，未改版本”；只有真实偏差才进入候选改造，并重跑失败样例与受影响旧例。

调度使用宿主原生能力，遵守已有授权与通知偏好。保存准确任务 ID、节奏和首轮实际运行证据；未创建前 `scheduler.state` 保持 `on_next_invocation`。同一任务默认低频，状态未变或无数据时安静，不反复发通知，不把其他巡检当成本技能的运行证明。

此轻量记录器按单个调用串行写入；多个代理应分别记录，汇总前去重，不能同时写同一个 JSON 文件。没有通用的全宿主自动采集器，也不承诺能看到用户在其他会话或设备的所有使用。

## 上游与实际使用分开

GitHub 来源更新另用 [upstream.md](upstream.md) 的注册表与检查器；`followup.py` 保持只检查明确登记的真实使用。上游 commit 变化不增加实际使用样本。升级本技能后建立新版本使用记录，保留旧记录，不把旧成功改标成新版成功。
