# 巨潮分红证据离线核验

`ashare_lab.dividend_evidence` 只读取已保留的请求、PDF 与人工事实配置，不联网、不更新旧候选。
当前搜索审计专用于 `cninfo-dividend-search-plan-v1`：既有条件成员缺请求的精确 95 只深市证券，
2023 年公告日期、“权益分派”关键词、空类别、每页 30 条、每只最多 2 页且不重试。
原始 336 只计划与 BaoStock 失败状态保持独立。新的 14 只类别查询不由这个接口认证。

在源码所在 worktree 使用已存在的项目解释器，显式区分源码与原件根目录：

```powershell
$env:PYTHONPATH = (Resolve-Path src).Path
$env:PYTHONIOENCODING = 'utf-8'
$taskPython = 'E:\量化\ashare-quant-lab\.venv\Scripts\python.exe'
$taskEvidenceRoot = 'E:\量化\ashare-quant-lab'
& $taskPython -m ashare_lab.dividend_evidence audit-search `
  --evidence-root $taskEvidenceRoot `
  --plan datasets/raw/cninfo/20261004-dividend-notices/plan.json `
  --manifest datasets/raw/cninfo/20261004-dividend-notices/run.json `
  --report .cache/dividend-search-audit.json
```

报告路径必须尚不存在。成功退出 0 只表示所选查询完整且原件关联核验通过，不表示全年事件覆盖。
完整非空、完整零结果、未请求、失败、不完整、损坏证据分别计数。`validated_rows` 是已通过页级来源与结构
检查的行数，可能包括不完整查询中保留的页，不能当作事件覆盖计数。请求未登记进 run 的捕获只供诊断，
不会晋升为完整查询。真实失败 / 未请求返回 2，证据矛盾返回 1；计划或 manifest 本身损坏时输出失败报告。
分页依据连续页号、稳定总数、公告唯一性和 `hasMore` 闭合；保留的 `totalpages=0` 不等价于零结果。

人工事实配置使用 `cninfo-reviewed-dividend-claims-v1`，每字段显式给出值、单位、口径及原页引文。
验证会从 PDF 重新提取全文，与已保留提取逐页比对，逐项核对搜索定位、证券代码、公告号、阶段、
落款与金额 / 日期角色。只支持已审阅公告的句式，不执行通用公告推断。

```powershell
& $taskPython -m ashare_lab.dividend_evidence validate-claims `
  --evidence-root $taskEvidenceRoot `
  --plan datasets/raw/cninfo/20261004-dividend-notices/plan.json `
  --manifest datasets/raw/cninfo/20261004-dividend-notices/run.json `
  --claims configs/dividend-evidence-2023-reviewed.json `
  --pdftotext D:\texlive\2025\bin\windows\pdftotext.exe `
  --report .cache/dividend-claims-validation.json
```

验证成功返回 0，但所有 `research_eligible=false`、`available_time=null`、`implementation_eligible=false`。
预案只能输出 `reviewed_proposal`，不能携带实施字段。缺失事实为 null，不补零；送转零值必须有明确否定语句
和本次实施与批准方案一致的引用。税后、支付范围及零碎股政策保留原文。
每十股现金除以十得到的算术值与正文每股值分别保存；差异单列。除权扣减使用单独字段。
所有 `canonical_cash_per_share` 保持 null，即使所选数值相符，也没有完成真实执行验收。

`legacy_actions_probe_v1` 仅适用于既有 300308 公告 1216935954 的确切 PDF / 元数据摘要。
它保留旧观察时间及缺少最终 URL、传输头信息的限制，不伪造新抓取或传输证明。

源码提交并保持干净后，把上面的 `validate-claims` 改为 `build-claims`，去掉 `--report`，追加：

```powershell
  --output datasets/candidates/dividend-reviewed-2023-v1
```

输出目录必须不存在且应位于被 Git 忽略的候选目录。构建前后核验干净提交、所有 `ashare_lab` 导入来自
同一源码根、源码与提交一致；开发态验证可以在未提交修改上运行。输出含 `candidates.json`、
`search_audit.json` 和记录文件摘要 / 源码身份的 `manifest.json`。
构建成功返回 **2**，表示候选已保留、研究资格仍受阻；损坏证据或来源身份不一致返回 1。
构建期间源码发生变化时失败，不把该批标成稳定产物。批次现有原件和报告均不覆盖。

Python 接口为 `audit_search_run(plan_path, manifest_path, evidence_root)`、
`validate_reviewed_claims(config_path, plan_path, manifest_path, evidence_root, pdftotext)` 和
`build_reviewed_candidates(..., output)`。输入路径相对 `evidence_root`；源码身份来自实际导入模块。
所有哈希证明的是本项目保留文件之间的关联，不是供应商签名或历史首次公开时间证明。
