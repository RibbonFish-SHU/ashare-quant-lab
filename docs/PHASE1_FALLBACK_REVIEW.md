# 补充行情主审

主代理，2026-10-03。执行分支 `phase1/source-fallback`，基于 `54d20bd`。
当前为工作稿预审；实施、回归、实际采集和最终集成结论待本批交付更新。

## 预审发现

1. 新采集的 HTTP 元数据需要明确 `classification=complete`、`transfer_complete=true`，
   且无 error / exception；仅有 HTTP 200 和合法 JSON 不足以覆盖采集器记录的失败。
   七份旧探针缺少新增字段，其兼容应绑定已审阅的原件摘要，不能把默认成功用于未知新捕获。
   主代理在独立 fixture 中把真实探针的元数据副本标为 protocol_error 并附 error，
   读取器仍返回 5 行。真实探针与响应未修改。
2. 有界 HTTP `read(n)` 遇到 EOF 仍须核对响应声明的 `Content-Length`。
   离线模拟声明 583 字节、实际 573 字节但 JSON 结构完整的响应，工作稿返回 complete，
   `transfer_complete=true`。短传输须保留失败证据并按有限重试策略处理。
3. 质量报告需区分未核实与通过：证券基本记录存在但上市 / 退市边界缺失时不得写边界通过；
   没有独立来源重叠样本时，零差异不等于交叉核对通过。

前两项离线复现保存在主仓库：

- `.cache/phase1/main-review/fallback-provenance-017ifohr/`，
  工作稿 pipeline SHA-256 `97a026bd6cb881e94fa72b924b5b5772ff7c88f5fe2d1709a5358708db360802`。
- `.cache/phase1/main-review/fallback-short-body-d_o1unac/`，
  工作稿 collect SHA-256 `00d94eabd661506662c9cbdb2ea11b8e8f33788eeff744c369baaa67de3ffe27`。

这些是人为构造的错误输入，不代表已保存的市场响应存在同样损坏；没有为复现访问行情服务。
执行代理已收到预审事项，联网前纳入修复与有效 / 无效边界回归。
