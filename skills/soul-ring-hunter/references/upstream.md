# 已吸收来源的上游更新

这是来源变化检查与有验证的维护入口。`upstream.py` 只读 GitHub；是否采用、候选验证和应用由代理继续执行既有工作流。它不是自动安装器，也不保证每次上游更新都有本地收益。

## 私有登记

注册表与状态文件放在技能目录之外，公开包只放示例，不带私人路径。

```json
{
  "schema": 1,
  "entries": [{
    "id": "writing-example",
    "repo": "owner/repository",
    "ref": "main",
    "paths": ["skills/example", "LICENSE"],
    "baseline_commit": null,
    "mode": "adapted",
    "decision": "update",
    "enabled": true,
    "interval_days": 7,
    "targets": ["existing-writing-skill"],
    "source_evidence": "records/original-absorption.md"
  }]
}
```

- `baseline_commit`：当初实际吸收所依据的40位提交，查不到就填 null，保持 `baseline_unknown`；不能把今天观察到的 HEAD 冒充过去的采用版本。
- `paths`：只登记实际参考或采用的文件/目录，最多8项；监控目录的 tree SHA 能反映其子文件变化。包括必要许可文件，不因仓库其他部分更新就升级本地。
- `targets`：本地目标名称，只作映射，不授权读取任意私有目录。一个上游机制可并入不同名字的本地技能。
- `mode`：`upstream` 表示直接使用，`adapted` 表示改造/机制借鉴；两者都必须验收后才能升级。`decision=reference` 仅观察来源，不能据此激活新技能；`decision` 为 `defer/reject`，或 `enabled=false` 的条目不执行检查。
- 关键来源默认7天，非关键30天。未登记来源不自动扫描；已有获准调度就合并使用，不逐技能新增计时器。

```sh
python3 <skill>/scripts/upstream.py check --registry <private>/registry.json --state <private>/state.json
# 初次实施/受影响修正的实际验证可 --force；例行调度不带 --force。
```

检查器解析分支到提交后读取固定提交的目录树，对比关注路径。只访问公开 GitHub API，不读取账号凭据、不执行远程指令。网络失败、限流、缺失/移动路径或目录树截断保留明确状态；未到期为 not_due，不等于本次联网确认无变化。历史采用基线与最近成功观察分别保存，报错不清除上一成功记录。

## 从发现更新到决定

1. 保留这次 JSON 检查结果，读取相关 diff 和必要说明；不用上游宣传或版本号代替证据。
2. 以旧上游、新上游、本地现状三方比较，明确有效增量及必须保留的定制。上游删除/重命名、依赖/权限/数据流或许可证变化要查清；先按实际影响判断，不自动解释为可安装升级。
3. 对值得采用且当前已授权的可逆部分，依主流程做方法试验、独立候选和新/旧用例验证，用 `skill_ops.py` 冻结、备份和应用。失败保留旧版，默认最多两轮定向修正。重大决定、身份验证、新权限或不可逆操作才需要最小输入。
4. 决策单独记录 `entry_id`、已评估的上游 commit/相关路径指纹、`adopted/partially_adopted/no_change/deferred/failed`、理由、候选/应用事务、测试证据、本地最终指纹及未采纳部分。仅查看、暂缓或不采用不能推进采用基线。
5. 部分采用可记录新的评估水位并保留旧采用记录；不自动宣称完整升级。被评估版本相同、本地内容和用途未变时复用旧决定；相同待办不反复通知。若实际完整采用并验收，备份注册表后更新基线并保留前后记录。

无变化和没有可行动变化时安静；告知实质完成、新失败或所需最小用户操作。上游 API/有限用例成功、调度配置保存、首次定时运行和长期收益分别验收。升级本技能后用新的 `followup.py` 使用记录承接，旧版样本不改标为新版。

接口依据：[GitHub Commits API](https://docs.github.com/en/rest/commits/commits)、[Git trees API](https://docs.github.com/en/rest/git/trees)。
