# 数据资产

本目录只保存开发和测试使用的虚构数据、Schema、生成/校验脚本及其报告。项目不连接生产订单、WMS或支付渠道，不产生真实业务副作用。

```text
data/
├── README.md
├── contracts/               # OpenAPI、MCP工具清单/响应Schema、事件Schema、指标审批登记与MySQL核心DDL
├── scenarios/expected-v2.schema.json
├── mock/v2/                 # PIM、OMS、WMS、支付等权威源模拟事实（合并种子文件，不是独立运行系统）
├── knowledge/v2/            # 有发布版本和证据的知识与图谱
├── scenarios/v2/            # 按FR/TC追溯的结构化验证场景
├── manifests/                # 版本、固定时钟、文件哈希及覆盖统计
├── scripts/                  # 确定性生成、静态校验与运行结果比较器
└── generated/reports/        # 可删除并重建的校验报告
```

旧`mock/v1`、`knowledge/v1`、`scenarios/v1`数据不再作为基线。v2生成器每次从固定种子完整重建其管理的数据目录，校验器输出写入`data/generated/reports/mock-v2-data-validation.json`。覆盖已有 v2 数据前应确认它是可丢弃的生成物；不得手工编辑生成文件后再把它作为新基线。

```powershell
node data/scripts/generate_mock_data.cjs
node data/scripts/validate_mock_data.cjs
# 校验本轮在既有v2原文旁新增的发布候选与物流事实；不重建原语料：
node data/scripts/build_knowledge_release.cjs
node data/scripts/validate_logistics_v2.cjs
# 详细规格的 TC 输入或已登记 HTTP 契约变更后，定向同步场景与清单；不重建业务事实：
node data/scripts/refresh_spec_derived_assets.cjs TC-35
# 应用测试执行器输出每例一行的实际事实后，比较器才可运行：
node data/scripts/evaluate_scenario_run.cjs <实际执行结果.jsonl>
```

校验器只做离线资产校验：验证结构化预期、manifest哈希、契约注册表、唯一标识、租户及跨系统归属、商品/订单/收货/退款金额关系、证据引用和FR↔TC↔case_id覆盖。通过不表示应用、Agent、数据库、WMS、支付、图谱或异常调查功能已经通过运行验收。

`mock/v2/systems.json`现含1000笔可复现的合成订单，平均分布于2个租户和10个规范商品；每笔都关联订单明细、商品编码、签收批次和支付明细，部分再关联退货收货/质检与退款流水。另含团队、渠道、区域等分析维度；1,000笔订单带来的跨系统业务记录总量及逐实体计数以`manifests/mock-v2.json`为准（当前7,094条）。新增`mock/v2/logistics.json`有2条明确的只读运单轨迹，与OMS签收时间一致，不改动上述订单与业务数组计数；哈希见`manifests/logistics-v2.json`。这不是真实生产数据。

固定业务时钟和规则版本记录在manifest。当前场景目录覆盖需求详细规格中的131个TC，共149条场景记录（含边界变体）；此前缺少的61个TC已按需求表补入测试输入、验收标准和流程步骤。补入的验收标准来自需求原文，仍属于设计夹具，并非可执行断言。`acceptance-test-map.csv`为每个TC分配预期测试标识和计划文件；其中静态`execution_status`尚未与实际应用测试逐项同步。当前已有应用源码和初步Harness，但149条业务场景尚未全量运行，不能据此计为通过；后续需逐项绑定运行断言并执行。

知识语料含10篇版本化合成文档（含FAQ）和7条有来源证据的图谱边。`knowledge-v2.json`记录原文哈希、每篇文档的候选切片ID与证据ID；`knowledge_labels.jsonl`含7条公开标注样例，仍需步骤17～20运行检索测评。公开保留集是冻结回归集，不是盲测集；每条场景必须在隔离重置环境执行setup，预期断言只供测试判定，不得进入Agent提示词、知识索引或业务接口。`generate_mock_data.cjs`已限定只清理其自有v2文件，重新生成前仍须先检查当前未提交的数据与清单，避免覆盖人工调整。

当前场景目录仍标记`design_only`，该字段尚未随新应用Harness逐项更新。`expected.tool_calls`仅记录注册MCP工具、真实业务参数和请求者权限；应用用例、编排、本体、WMS及支付步骤记在`expected.workflow_steps`。离线校验器检查工具注册、参数Schema、权限、来源事实、金额关系和FR/TC/case追溯；静态通过不等于场景运行或端到端验收通过。所有外部副作用均为模拟记录，生产凭证不得写入本目录。
