// Offline checks for generated v2 fixture shape, ownership, traceability and hashes.
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');

const dataRoot = path.resolve(__dirname, '..');
const projectRoot = path.resolve(dataRoot, '..');
const read = (relativePath) => JSON.parse(fs.readFileSync(path.join(dataRoot, relativePath), 'utf8'));
const hash = (absolutePath) => crypto.createHash('sha256').update(fs.readFileSync(absolutePath)).digest('hex');
const errors = [];
let checks = 0;
const check = (ok, message) => { checks += 1; if (!ok) errors.push(message); };

const schema = read('scenarios/expected-v2.schema.json');
const systems = read('mock/v2/systems.json');
const catalog = read('scenarios/v2/catalog.json');
const manifest = read('manifests/mock-v2.json');
const metricDefinitions = read('knowledge/v2/ontology/metrics.json');
const metricApprovalRegister = read('contracts/metric-approval-register.json');
const knowledgeCorpus = read('knowledge/v2/corpus.json');
const toolManifest = read('contracts/mcp-tools.json');
const toolResponseSchema = read('contracts/mcp-response.schema.json');
const eventCatalog = read('contracts/event-catalog.json');
const securityBaseline = read('contracts/security-baseline.json');
const ontologyCore = fs.readFileSync(path.join(dataRoot,'knowledge/v2/ontology/core.ttl'),'utf8');
const ontologyShapes = fs.readFileSync(path.join(dataRoot,'knowledge/v2/ontology/constraints.shacl.ttl'),'utf8');
const openApiText = fs.readFileSync(path.join(dataRoot, 'contracts/openapi.yaml'), 'utf8');
const evidence = new Map([...systems.anomaly_evidence, ...knowledgeCorpus.evidence].map((row) => [row.evidence_id, row]));
const scenarios = fs.readFileSync(path.join(dataRoot, 'scenarios/v2/multi_system.jsonl'), 'utf8').trim().split(/\r?\n/).map(JSON.parse);
const requiredTcs = [2,3,31,32,33,...Array.from({length:29},(_,i)=>95+i)].map((n)=>`TC-${String(n).padStart(2,'0')}`);

check(schema.$id === 'urn:after-sales-agent:expected-v2', 'expected v2 Schema 标识不正确');
check(systems.synthetic_only === true && systems.dataset_version === manifest.dataset_version, '模拟事实未标记为虚构数据或版本不一致');
check(systems.synthetic_batch?.synthetic_only===true&&systems.synthetic_batch?.target_orders===1000&&systems.synthetic_batch?.generated_orders===1000, '跨系统模拟订单批次必须包含恰好1000条可追溯的合成订单');
check(systems.orders.filter((row)=>row.synthetic===true).length===1000, '合成订单事实数量不是1000');
check(new Set(systems.orders.filter((row)=>row.synthetic===true).map((row)=>`${row.tenant_id}|${row.order_id}`)).size===1000, '合成订单主键重复');
const syntheticOrders=systems.orders.filter((row)=>row.synthetic===true);
check(systems.synthetic_batch.tenants===2&&systems.synthetic_batch.products===10, '合成订单批次的租户或商品覆盖数不符');
for (const order of syntheticOrders) {
  const line=systems.order_lines.find((row)=>row.tenant_id===order.tenant_id&&row.order_id===order.order_id);
  const shipment=systems.shipment_batches.find((row)=>row.tenant_id===order.tenant_id&&row.order_id===order.order_id);
  const payment=systems.payments.find((row)=>row.tenant_id===order.tenant_id&&row.order_id===order.order_id);
  const detail=systems.payment_details.find((row)=>row.tenant_id===order.tenant_id&&row.order_id===order.order_id);
  check(!!line&&!!shipment&&!!payment&&!!detail&&line.product_id===shipment.product_id&&detail.product_id===shipment.product_id, `${order.order_id} 的订单、商品、签收与支付事实未完整关联`);
  check(['web','marketplace','retail','partner'].includes(order.sales_channel)&&['east','south','west','north'].includes(order.sales_region)&&/^team-(n|s)-[1-3]$/.test(order.team_id), `${order.order_id} 缺少有效渠道/区域/团队分析维度`);
}
for (const product of systems.products.filter((row)=>row.synthetic===true)) check(syntheticOrders.filter((row)=>row.tenant_id===product.tenant_id&&systems.order_lines.some((line)=>line.tenant_id===row.tenant_id&&line.order_id===row.order_id&&line.product_id===product.canonical_product_id)).length===100, `${product.canonical_product_id} 未生成100笔合成订单`);
check(catalog.total_cases === scenarios.length && manifest.counts.scenarios === scenarios.length, '场景清单计数不一致');
check(catalog.execution_readiness==='design_only'&&catalog.execution_blockers?.some((item)=>item.includes('No application source code'))&&catalog.execution_blockers?.some((item)=>item.includes('not executable acceptance tests')), '场景目录不得把设计夹具标记为可执行/已通过验收');
check(manifest.counts.ontology_files===4&&manifest.counts.mock_fact_files===1&&manifest.counts.source_systems.length===5&&manifest.files.some((row)=>row.path==='data/contracts/metric-approval-register.json')&&manifest.files.some((row)=>row.path==='data/scenarios/v2/acceptance-test-map.csv'), '数据清单缺少本体/来源命名空间/指标审批台账/完整验收映射资产');
check(ontologyCore.includes('urn:after-sales-agent:ontology:v2#')&&!ontologyCore.includes('example.invalid')&&ontologyCore.includes(':Product a owl:Class ; owl:equivalentClass :CanonicalProduct'), 'OWL命名空间或Product/CanonicalProduct统一语义异常');
for (const shape of [':CanonicalProductShape',':SourceProductMappingShape',':EvidenceShape',':MetricDefinitionShape',':OntologyReleaseShape']) check(ontologyShapes.includes(shape), `SHACL缺少核心形状 ${shape}`);
check(ontologyShapes.includes('sh:sparql')&&ontologyShapes.includes('COUNT(DISTINCT ?target)')&&ontologyShapes.includes(':validFrom'), 'SHACL缺少歧义映射候选数或映射时效约束');
check(!/:targetClass :(Order|OrderLine|ShipmentBatch|ReturnAuthorization|WarehouseReceipt|Inspection|Payment|RefundExecution|MetricSnapshot|AnomalyInvestigation)\b/.test(ontologyShapes), 'SHACL不得要求将动态交易个体导入知识图谱');
check(knowledgeCorpus.schema_version==='2.0'&&knowledgeCorpus.synthetic_only===true&&knowledgeCorpus.dataset_version===manifest.dataset_version&&knowledgeCorpus.documents.length>=8, '知识语料缺少版本/虚构标记或代表性文档');
check(['synonym','typo','multi_hop','expired_version','draft_conflict','long_tail','no_answer','tenant_scope'].every((tag)=>knowledgeCorpus.coverage_tags.includes(tag)), '知识语料缺少检索边界覆盖标签');
check(new Set(knowledgeCorpus.documents.map((doc)=>`${doc.tenant_id}|${doc.document_id}`)).size===knowledgeCorpus.documents.length, '知识文档ID在租户内重复');
for (const doc of knowledgeCorpus.documents) check(['published','draft','retired'].includes(doc.status)&&doc.valid_from&&doc.evidence_id, `${doc.document_id} 缺少有效状态/日期/证据`);
const graphNodes = new Map(knowledgeCorpus.graph.nodes.map((node)=>[`${node.tenant_id}|${node.node_id}`,node]));
for (const edge of knowledgeCorpus.graph.edges) {
  check(graphNodes.has(`${edge.tenant_id}|${edge.from}`)&&graphNodes.has(`${edge.tenant_id}|${edge.to}`), `${edge.edge_id} 图谱边跨租户或端点不存在`);
  check(knowledgeCorpus.documents.some((doc)=>doc.tenant_id===edge.tenant_id&&doc.document_id===edge.document_id), `${edge.edge_id} 缺少同租户来源文档`);
  check(evidence.has(edge.evidence_id)&&evidence.get(edge.evidence_id).tenant_id===edge.tenant_id, `${edge.edge_id} 缺少同租户证据`);
}
check(knowledgeCorpus.graph.edges.some((first)=>knowledgeCorpus.graph.edges.some((second)=>first.tenant_id===second.tenant_id&&first.to===second.from)), '知识图谱没有可验证的两跳关系样例');
for (const item of knowledgeCorpus.evidence) check(knowledgeCorpus.documents.some((doc)=>doc.tenant_id===item.tenant_id&&doc.document_id===item.document_id&&doc.evidence_id===item.evidence_id&&doc.status===item.status), `${item.evidence_id} 证据与知识文档不一致`);
const openApiPaths = [...openApiText.matchAll(/^  (\/[^:]+):\s*$/gm)].map((match)=>match[1]);
const requiredHttpPaths = [
  '/chat','/tasks/{task_id}','/tickets','/tickets/{ticket_id}','/tickets/{ticket_id}/messages','/ticket-confirmations','/handoffs',
  '/health/live','/health/ready','/auth/mock/session','/me','/workbench/customers','/workbench/tickets',
  '/workbench/tickets/{ticket_id}/assignment','/workbench/tickets/{ticket_id}/status','/workbench/handoffs',
  '/workbench/handoffs/{handoff_id}/accept','/workbench/refund-requests','/workbench/refund-requests/{refund_request_id}/review',
  '/workbench/audit','/workbench/after-sales-metrics','/workbench/returns/{return_id}/receipts',
  '/workbench/returns/{return_id}/inspections','/workbench/refund-executions/{execution_id}',
  '/workbench/refund-executions/{execution_id}/execute','/investigations','/investigations/{investigation_id}',
  '/investigations/{investigation_id}/revisions','/refund-requests/{refund_request_id}',
  '/orders/{order_id}/address-changes','/orders/{order_id}/address-changes/{address_change_id}/confirm',
  '/orders/{order_id}/address-changes/{address_change_id}','/orders/{order_id}/refund-quotes/{quote_id}/confirm',
  '/orders/{order_id}','/after-sales/metrics'
];
check(new Set(openApiPaths).size===openApiPaths.length, 'OpenAPI路径存在重复定义');
for (const route of requiredHttpPaths) check(openApiPaths.includes(route), `OpenAPI缺少已批准HTTP入口 ${route}`);
check(openApiPaths.length===requiredHttpPaths.length, 'OpenAPI包含未经技术规格登记的额外路径，请先更新入口清单');
const openApiOperationIds=[...openApiText.matchAll(/^      operationId: ([A-Za-z][A-Za-z0-9]+)$/gm)].map((match)=>match[1]);
const requiredHttpOperationIds=['submitChat','getTask','createTicket','getTicket','addTicketMessage','createTicketConfirmation','createHandoff','healthLive','healthReady','createMockSession','getCurrentIdentity','listAuthorizedCustomers','listAuthorizedTickets','assignTicket','changeTicketStatus','listAuthorizedHandoffs','acceptHandoff','listReviewableRefunds','reviewRefund','queryMaskedAudit','queryWorkbenchAfterSalesMetrics','recordReturnReceipt','recordReturnInspection','queryRefundExecution','executeRefund','getInvestigation','reviseInvestigation','getRefundRequest','createAddressChange','confirmAddressChange','getAddressChange','confirmRefundQuote','queryOrder','queryAfterSalesMetrics','createInvestigation'];
check(new Set(openApiOperationIds).size===openApiOperationIds.length, 'OpenAPI operationId存在重复定义');
check(requiredHttpOperationIds.every((operationId)=>openApiOperationIds.includes(operationId))&&openApiOperationIds.length===requiredHttpOperationIds.length, 'OpenAPI HTTP操作清单与批准入口不一致');
const operationBlocks=[...openApiText.matchAll(/^      operationId: ([A-Za-z][A-Za-z0-9]+)$/gm)].map((match,index,all)=>({id:match[1],start:match.index,end:all[index+1]?.index??openApiText.length}));
for (const operation of operationBlocks) {
  const block=openApiText.slice(operation.start,operation.end);
  const responseEntries=[...block.matchAll(/^\s+'(\d{3})':/gm)].map((match,index,all)=>({status:match[1],start:match.index,end:all[index+1]?.index??block.length}));
  const successStatuses=responseEntries.filter((row)=>row.status.startsWith('2'));
  check(successStatuses.length>0, `${operation.id} 未定义成功响应`);
  for (const entry of responseEntries) {
    const response=block.slice(entry.start,entry.end);
    if (entry.status.startsWith('2')) check(response.includes('content:')&&response.includes('schema:')&&response.includes('#/components/schemas/'), `${operation.id} ${entry.status} 成功响应缺少机器可读DTO`);
    if (entry.status.startsWith('4')||entry.status.startsWith('5')) check(response.includes('$ref:')||response.includes('content:')&&response.includes('schema:'), `${operation.id} ${entry.status} 错误响应缺少机器可读Schema或共享响应引用`);
  }
}
const schemaSection=openApiText.split(/^  schemas:\s*$/m)[1]||'';
const openApiSchemaNames=new Set([...schemaSection.matchAll(/^    ([A-Za-z][A-Za-z0-9]+):/gm)].map((match)=>match[1]));
for (const ref of openApiText.matchAll(/#\/components\/schemas\/([A-Za-z][A-Za-z0-9]+)/g)) check(openApiSchemaNames.has(ref[1]), `OpenAPI引用了不存在的Schema ${ref[1]}`);
const toolNames = toolManifest.tools.map((row)=>row.name);
check(toolManifest.schema_version==='1.0'&&new Set(toolNames).size===toolNames.length&&toolManifest.response_schema_ref==='data/contracts/mcp-response.schema.json', 'MCP工具清单版本错误、工具名重复或响应Schema未绑定');
check(toolResponseSchema.$id==='urn:after-sales-agent:mcp-response:v1'&&toolResponseSchema.required.includes('invocation_id')&&toolResponseSchema.required.includes('data'), 'MCP响应Schema缺少稳定标识或结果载荷');
check(!toolManifest.tools.some((row)=>row.name==='execute_refund'||row.name==='record_return_receipt'||row.name==='record_return_inspection'), '模型MCP清单暴露了支付/WMS执行工具');
check(toolResponseSchema.properties.data.anyOf?.some((item)=>item.type==='null')&&toolResponseSchema.allOf.some((entry)=>entry.if?.properties?.status?.const==='error'&&entry.then?.properties?.data?.type==='null'), 'MCP错误响应未与成功结果DTO区分');
const responseBindings = new Map((toolResponseSchema.allOf||[]).map((entry)=>[entry.if?.properties?.tool_name?.const,entry.then?.properties?.data?.$ref]).filter(([name])=>!!name));
for (const tool of toolManifest.tools) {
  check(tool.input_schema?.type==='object'&&tool.input_schema.additionalProperties===false, `${tool.name}缺少严格对象输入Schema`);
  const expectedRef=`data/contracts/mcp-response.schema.json#/$defs/tool_data_${tool.name}`;
  const dto=toolResponseSchema.$defs?.[`tool_data_${tool.name}`];
  check(tool.response_schema_ref===expectedRef&&!!dto, `${tool.name}缺少独立输出DTO或引用无效`);
  check(dto?.type==='object'&&dto.additionalProperties===false&&dto.required?.length>0&&dto.required.every((field)=>Object.hasOwn(dto.properties||{},field)), `${tool.name}输出DTO不是严格对象Schema或必填字段未定义`);
  const binding=(toolResponseSchema.allOf||[]).find((entry)=>entry.if?.properties?.tool_name?.const===tool.name&&entry.then?.properties?.data?.$ref);
  check(responseBindings.get(tool.name)===`#/$defs/tool_data_${tool.name}`&&binding?.if?.properties?.status?.enum?.includes('success'), `${tool.name}未在MCP响应Schema中按tool_name绑定输出DTO`);
}
for (const requiredTool of ['search_knowledge_base','query_knowledge_graph','search_hybrid_knowledge','get_policy_detail','query_logistics','query_tracking','check_delivery_exception','check_shipping_address_change','update_shipping_address','create_refund_request','query_refund_request','create_ticket','query_ticket','handoff_to_human','update_ticket','query_return_progress','query_refund_execution','query_after_sales_metrics','explain_anomaly']) check(toolNames.includes(requiredTool), `MCP工具清单缺少 ${requiredTool}`);
check(eventCatalog.schema_version==='1.0'&&eventCatalog.exchange==='after_sales.events'&&eventCatalog.dead_letter_queue==='after_sales.dlq', '事件目录缺少版本、统一交换机或DLQ');
check(new Set(eventCatalog.events.map((row)=>row.event_type)).size===eventCatalog.events.length, '事件目录存在重复event_type');
for (const event of eventCatalog.events) check(event.queue&&event.producer&&event.consumer&&event.payload_required.length>0, `${event.event_type}缺少队列、生产者、消费者或payload字段`);
const catalogEventTypes=new Set(eventCatalog.events.map((row)=>row.event_type));
check(new Set(systems.events.map((row)=>row.event_id)).size===systems.events.length, '模拟源事件ID重复');
for (const event of systems.events) check(catalogEventTypes.has(event.type), `${event.event_id} 使用了事件目录之外的类型 ${event.type}`);
check(securityBaseline.identity.validate_audience_per_resource===true&&securityBaseline.identity.token_passthrough===false&&securityBaseline.identity.authorize_each_tool_call===true, '安全基线缺少audience、禁止token passthrough或逐工具授权');
check(securityBaseline.model.production_default==='private_endpoint'&&securityBaseline.model.cloud_enabled_by_default===false, '生产模型默认设置不符合私有模型和云API审批门槛');
check(securityBaseline.payment_data.store_pan===false&&securityBaseline.payment_data.store_cvv===false, '安全基线不得保存PAN或CVV');
check(new Set(scenarios.map((row)=>row.case_id)).size === scenarios.length, 'case_id重复');
for (const tc of requiredTcs) check(scenarios.some((row)=>row.tc_id===tc), `缺少必需场景 ${tc}`);
check(catalog.development_cases + catalog.holdout_cases === scenarios.length && catalog.holdout_is_blind === false, '开发集/公开保留集划分不完整或错误宣称为盲测');
for (const tc of ['TC-124','TC-125','TC-126','TC-127','TC-128']) check(scenarios.some((row)=>row.tc_id===tc&&row.requirement_ids.includes('FR-04')), `知识检索边界缺少 ${tc}`);
check(catalog.requirement_tc_count===131&&catalog.mapped_tc_count===131&&new Set(scenarios.map((row)=>row.tc_id)).size===131, '需求TC必须全部映射到至少一个场景');
const caseIds = new Set(scenarios.map((row)=>row.case_id));

for (const [index, scenario] of scenarios.entries()) {
  const label = scenario.case_id || `scenario-${index}`;
  check(scenario.schema_version === '2.0' && scenario.dataset_version === manifest.dataset_version, `${label} 数据版本不匹配`);
  check(scenario.synthetic_only === true && scenario.setup.reset_to_seed === true, `${label} 缺少隔离重置/虚构标记`);
  check(scenario.requirement_ids.length > 0 && /^TC-\d+[A-Z]?$/.test(scenario.tc_id), `${label} 缺少FR/TC映射`);
  check(['development','holdout'].includes(scenario.partition), `${label} 缺少开发集/公开保留集分区`);
  if (scenario.variant_of) check(caseIds.has(scenario.variant_of) && scenario.variant_of !== scenario.case_id, `${label} 变体引用不存在或自引用`);
  for (const override of scenario.setup.overrides || []) check(override.tenant_id === scenario.tenant_id && typeof override.overlay_id === 'string', `${label} setup override缺少同租户隔离标识`);
  const expected = scenario.expected;
  for (const key of schema.required) check(Object.hasOwn(expected, key), `${label} expected 缺少 ${key}`);
  check(expected.assertion_mode === 'structured_facts_tool_calls_effects_evidence_state', `${label} assertion_mode 不正确`);
  check(Array.isArray(expected.facts) && (expected.facts.length > 0 || Array.isArray(expected.acceptance_criteria)&&expected.acceptance_criteria.length>0), `${label} 缺少机器事实或来源明确的验收标准`);
  if (expected.acceptance_criteria) check(expected.acceptance_criteria.every((item)=>typeof item==='string'&&item.trim().length>0), `${label} 验收标准为空或类型错误`);
  for (const fact of expected.facts) check(typeof fact.path === 'string' && typeof fact.operator === 'string' && Object.hasOwn(fact,'value'), `${label} fact 断言字段不完整`);
  check(expected.tool_calls && ['must','may','must_not'].every((key)=>Array.isArray(expected.tool_calls[key])), `${label} 工具调用预期不完整`);
  check(expected.workflow_steps && ['must','may'].every((key)=>Array.isArray(expected.workflow_steps[key])), `${label} workflow_steps 预期不完整`);
  const identities = scenario.identity_context?.execution_identities || {};
  check(scenario.identity_context?.requester_ref === 'requester' && identities.requester?.identity_type === 'human', `${label} 缺少独立请求者身份`);
  const registeredTools = new Map(toolManifest.tools.map((tool)=>[tool.name,tool]));
  for (const call of [...(expected.tool_calls?.must||[]),...(expected.tool_calls?.may||[]),...(expected.tool_calls?.must_not||[])]) {
    const definition = registeredTools.get(call.tool_name);
    check(!!definition, `${label} MCP工具未注册: ${call.tool_name}`);
    check(call.identity_ref === 'requester', `${label} MCP调用身份必须引用请求者授权上下文`);
    if (!definition) continue;
    const args = call.arguments || {};
    const input = definition.input_schema || {};
    for (const required of input.required || []) check(Object.hasOwn(args,required), `${label} ${call.tool_name} 缺少参数 ${required}`);
    if (input.additionalProperties === false) for (const key of Object.keys(args)) check(Object.hasOwn(input.properties||{},key), `${label} ${call.tool_name} 存在未声明参数 ${key}`);
    for (const [key,value] of Object.entries(args)) {
      const rule=input.properties?.[key]; if (!rule) continue;
      if (rule.type==='string') check(typeof value==='string'&&value.length>=(rule.minLength||0)&&value.length<=(rule.maxLength||Infinity), `${label} ${call.tool_name}.${key} 字符串类型/长度不符合Schema`);
      if (rule.type==='integer') check(Number.isInteger(value)&&(rule.minimum===undefined||value>=rule.minimum)&&(rule.maximum===undefined||value<=rule.maximum), `${label} ${call.tool_name}.${key} 整数范围不符合Schema`);
      if (rule.type==='array') {
        check(Array.isArray(value)&&(rule.minItems===undefined||value.length>=rule.minItems)&&(rule.maxItems===undefined||value.length<=rule.maxItems), `${label} ${call.tool_name}.${key} 数组长度不符合Schema`);
        if (Array.isArray(value)&&rule.items?.enum) for (const item of value) check(rule.items.enum.includes(item), `${label} ${call.tool_name}.${key} 项不属于Schema枚举`);
        if (Array.isArray(value)&&rule.items?.type==='string') for (const item of value) check(typeof item==='string'&&item.length>=(rule.items.minLength||0)&&item.length<=(rule.items.maxLength||Infinity), `${label} ${call.tool_name}.${key} 项类型/长度不符合Schema`);
      }
      if (rule.enum) check(rule.enum.includes(value), `${label} ${call.tool_name}.${key} 不属于Schema枚举`);
      if (rule.format==='date-time') check(typeof value==='string'&&!Number.isNaN(Date.parse(value)), `${label} ${call.tool_name}.${key} 不是有效日期时间`);
    }
    for (const scope of definition.auth_scopes||[]) check((identities.requester?.scopes||[]).includes(scope), `${label} 请求者缺少 ${call.tool_name} 所需权限 ${scope}`);
  }
  check(JSON.stringify(identities.requester?.scopes||[])===JSON.stringify(scenario.actor?.scopes||[]), `${label} 请求者权限与场景actor声明不一致`);
  for (const step of [...(expected.workflow_steps?.must||[]),...(expected.workflow_steps?.may||[])]) {
    check(typeof step.operation_id==='string'&&!step.operation_id.includes('tc_id'), `${label} workflow step 仍使用用例占位参数`);
    check(Object.hasOwn(identities,step.identity_ref), `${label} workflow step 引用未声明身份 ${step.identity_ref}`);
    if (step.identity_ref==='warehouse_operator') check(['warehouse:receive','warehouse:inspect'].every((scope)=>identities.warehouse_operator.scopes.includes(scope)), `${label} 仓储员工权限不完整`);
    if (step.identity_ref==='payment_service') check(['payment:execute','payment:callback:verify'].every((scope)=>identities.payment_service.scopes.includes(scope)), `${label} 支付服务权限不完整`);
  }
  check(Array.isArray(expected.effects) && expected.effects.length > 0, `${label} 没有副作用断言`);
  check(Array.isArray(expected.forbidden) && expected.forbidden.includes('real_payment_or_warehouse_side_effect'), `${label} 缺少真实副作用禁止断言`);
  check(expected.state && ['review_status','return_status','funds_status'].every((key)=>Object.hasOwn(expected.state,key)), `${label} 三态预期不完整`);
  check(expected.reference_time === manifest.reference_time, `${label} 固定业务时钟不一致`);
  for (const evidenceId of expected.evidence_refs) {
    const ref = evidence.get(evidenceId);
    check(!!ref && ref.tenant_id === scenario.tenant_id, `${label} 证据不存在或跨租户`);
    check(scenario.seed_refs.evidence_refs.includes(evidenceId), `${label} seed_refs 未声明预期证据`);
  }
}

const tenants = new Set(systems.tenants.map((row)=>row.tenant_id));
const metricDefinitionIds = new Set(metricDefinitions.map((row)=>row.metric_id));
const eventIds = new Set(systems.events.map((row)=>row.event_id));
check(metricDefinitions.length===4&&metricDefinitionIds.size===4&&metricDefinitionIds.has('return_passed_item_rate')&&metricDefinitionIds.has('return_application_item_rate'), '指标字典必须分别登记验收通过退货件数率和退货申请率，并包含两项退款指标');
for (const definition of metricDefinitions) check(definition.business_approval==='pending'&&definition.minimum_sample===100&&definition.window_days===30&&(definition.metric_id==='return_application_item_rate'?definition.sample_unit===null:!!definition.sample_unit), `${definition.metric_id} 缺少统一100样本/30日窗口或审批状态`);
const returnDefaultMetric=metricDefinitions.find((row)=>row.metric_id==='return_passed_item_rate');
const applicationMetric=metricDefinitions.find((row)=>row.metric_id==='return_application_item_rate');
check(returnDefaultMetric?.ranking_role==='supervisor_default_candidate'&&returnDefaultMetric.default_after_approval===true&&returnDefaultMetric.production_enabled===false, '主管默认排名候选必须是待业务/财务批准的验收通过退货件数率');
check(applicationMetric?.ranking_role==='independent_metric'&&applicationMetric.default_after_approval===false&&applicationMetric.production_enabled===false&&applicationMetric.mock_evaluable===false&&applicationMetric.formula===null&&applicationMetric.calculation_status==='blocked_pending_business_finance_approval'&&applicationMetric.numerator_options?.length===3&&applicationMetric.denominator_options?.length===3&&applicationMetric.cohort_time_options?.length===3, '退货申请率必须作为独立待审批指标登记，未批准时不得计算或排名');
check(!systems.metric_snapshots.some((row)=>row.metric_id==='return_application_item_rate'), '未批准的退货申请率不得存在已计算指标快照');
const approvalByMetric=new Map((metricApprovalRegister.metrics||[]).map((row)=>[row.metric_id,row]));
check(metricApprovalRegister.schema_version==='1.0'&&approvalByMetric.size===4&&metricApprovalRegister.metrics.every((row)=>metricDefinitionIds.has(row.metric_id)), '指标审批台账必须逐项对应四个指标定义');
for (const definition of metricDefinitions) {
  const approval=approvalByMetric.get(definition.metric_id);
  const fullyApproved=approval?.business_approval?.status==='approved'&&approval?.finance_approval?.status==='approved'&&!!approval.business_approval.approver&&!!approval.finance_approval.approver&&!!approval.business_approval.evidence_ref&&!!approval.finance_approval.evidence_ref;
  check(approval?.production_enabled===fullyApproved&&definition.production_enabled===fullyApproved, `${definition.metric_id} 未经业务/财务双重可追溯审批不得生产启用`);
}
for (const collection of ['products','product_mappings','orders','order_lines','shipment_batches','return_authorizations','warehouse_receipts','warehouse_inspections','payments','payment_details','refund_details','refund_executions','metric_snapshots','events','traces','anomaly_evidence']) {
  for (const row of systems[collection]) check(tenants.has(row.tenant_id), `${collection} 存在未知租户`);
}
const businessRecordCollections=['products','product_mappings','orders','order_lines','shipment_batches','return_authorizations','warehouse_receipts','warehouse_inspections','payments','payment_details','refund_details','refund_executions','metric_snapshots','events','traces','anomaly_evidence'];
const currentBusinessRecordCounts=Object.fromEntries(businessRecordCollections.map((name)=>[name,systems[name].length]));
const currentBusinessRecordTotal=Object.values(currentBusinessRecordCounts).reduce((sum,count)=>sum+count,0);
check(manifest.counts.synthetic_order_bundles===1000&&manifest.counts.business_records===currentBusinessRecordTotal&&JSON.stringify(manifest.counts.business_record_counts)===JSON.stringify(currentBusinessRecordCounts), 'manifest跨系统业务记录数量与模拟事实文件不一致');
const productKeys = new Set(systems.products.map((row)=>`${row.tenant_id}|${row.canonical_product_id}`));
for (const mapping of systems.product_mappings) check(productKeys.has(`${mapping.tenant_id}|${mapping.canonical_product_id}`), `${mapping.source_system}/${mapping.source_id} 指向其他租户或不存在的canonical产品`);
const mappingsBySource = new Map();
for (const mapping of systems.product_mappings) {
  const key = `${mapping.tenant_id}|${mapping.source_system}|${mapping.source_id}`;
  const group = mappingsBySource.get(key) || [];
  group.push(mapping);
  mappingsBySource.set(key,group);
}
for (const [key, mappings] of mappingsBySource) {
  check(mappings.every((mapping)=>['active','ambiguous','unmapped','expired'].includes(mapping.status)), `${key} has an invalid mapping status`);
  for (const mapping of mappings) {
    check(/^\d{4}-\d{2}-\d{2}$/.test(mapping.valid_from)&&(!mapping.valid_to||/^\d{4}-\d{2}-\d{2}$/.test(mapping.valid_to)), `${key} 映射有效期不是日期`);
    check(!mapping.valid_to||mapping.valid_from<mapping.valid_to, `${key} 映射有效区间为空或反向`);
  }
  const intervalKey = (mapping)=>`${mapping.valid_from}|${mapping.valid_to||'infinity'}`;
  const ambiguousIntervals = new Map();
  for (const mapping of mappings.filter((row)=>row.status==='ambiguous')) {
    const group=ambiguousIntervals.get(intervalKey(mapping))||new Set(); group.add(mapping.canonical_product_id); ambiguousIntervals.set(intervalKey(mapping),group);
  }
  for (const [interval,candidates] of ambiguousIntervals) check(candidates.size>=2, `${key} ${interval} ambiguous interval needs at least two distinct candidates`);
  const activeMappings=mappings.filter((row)=>row.status==='active');
  const overlaps=(a,b)=>a.valid_from<(b.valid_to||'9999-12-31')&&b.valid_from<(a.valid_to||'9999-12-31');
  for (let i=0;i<activeMappings.length;i++) for (let j=i+1;j<activeMappings.length;j++) check(!overlaps(activeMappings[i],activeMappings[j]), `${key} has overlapping active mapping intervals`);
}
for (const order of systems.orders) {
  const lines = systems.order_lines.filter((row)=>row.tenant_id===order.tenant_id&&row.order_id===order.order_id);
  check(lines.length > 0, `${order.tenant_id}/${order.order_id} 缺少订单明细`);
  check(lines.reduce((sum,row)=>sum+row.goods_minor+row.shipping_minor,0)===order.paid_minor, `${order.tenant_id}/${order.order_id} 明细金额与订单实付不守恒`);
  for (const payment of systems.payments.filter((row)=>row.order_id===order.order_id&&row.tenant_id===order.tenant_id)) check(payment.currency===order.currency&&payment.paid_minor<=order.paid_minor, `${payment.payment_id} 币种或金额与订单不符`);
}
for (const receipt of systems.warehouse_receipts) {
  const rma = systems.return_authorizations.find((row)=>row.tenant_id===receipt.tenant_id&&row.rma_id===receipt.rma_id);
  const inspection = systems.warehouse_inspections.find((row)=>row.tenant_id===receipt.tenant_id&&row.receipt_id===receipt.receipt_id);
  check(!!rma&&!!inspection, `${receipt.receipt_id} 缺少退货授权或质检记录`);
  if (rma&&inspection) check(inspection.passed_quantity+inspection.rejected_quantity===receipt.inspected_quantity&&receipt.received_quantity<=rma.authorized_quantity, `${receipt.receipt_id} 实收/质检数量不守恒或超过授权量`);
}
for (const refund of systems.refund_executions) {
  const payment = systems.payments.find((row)=>row.tenant_id===refund.tenant_id&&row.payment_id===refund.payment_id&&row.order_id===refund.order_id);
  check(!!payment&&refund.currency===payment.currency&&refund.amount_minor<=payment.paid_minor, `${refund.refund_id} 未匹配原支付或超过支付金额`);
  check(!!refund.idempotency_key&&!!refund.source_record_id, `${refund.refund_id} 缺少幂等或来源记录`);
}
for (const payment of systems.payments) {
  const details = systems.payment_details.filter((row)=>row.tenant_id===payment.tenant_id&&row.payment_id===payment.payment_id);
  if (details.length) {
    check(details.reduce((sum,row)=>sum+row.paid_minor,0)===payment.paid_minor, `${payment.payment_id} 支付明细加总不等于支付流水`);
    check(details.every((row)=>row.paid_goods_minor+row.paid_shipping_minor===row.paid_minor), `${payment.payment_id} 支付明细货款/运费不守恒`);
  }
  const refunds = systems.refund_executions.filter((row)=>row.tenant_id===payment.tenant_id&&row.payment_id===payment.payment_id&&row.status==='succeeded');
  check(refunds.reduce((sum,row)=>sum+row.amount_minor,0)<=payment.paid_minor, `${payment.payment_id} 成功退款累计超过实付`);
}
check(new Set(systems.refund_details.map((row)=>row.payment_detail_id)).size===systems.refund_details.length, '同一支付明细被重复计入成功退款率');
for (const detail of systems.refund_details) {
  const paymentDetail = systems.payment_details.find((row)=>row.tenant_id===detail.tenant_id&&row.payment_detail_id===detail.payment_detail_id);
  check(!!paymentDetail&&detail.refunded_goods_minor<=paymentDetail.paid_goods_minor, `${detail.refund_detail_id} 未匹配支付明细或退款货款超额`);
  check(systems.refund_executions.some((row)=>row.tenant_id===detail.tenant_id&&row.refund_id===detail.refund_id&&row.status==='succeeded'), `${detail.refund_detail_id} 缺少权威成功退款流水`);
}
for (const snapshot of systems.metric_snapshots) {
  check(metricDefinitionIds.has(snapshot.metric_id), `${snapshot.snapshot_id} 未引用指标字典`);
  check(eventIds.has(snapshot.source_watermark), `${snapshot.snapshot_id} 源水位不存在`);
  check(snapshot.denominator>=0&&snapshot.numerator>=0&&snapshot.numerator<=snapshot.denominator, `${snapshot.snapshot_id} 分子分母非法`);
  if (snapshot.denominator===0) check(snapshot.rate===null, `${snapshot.snapshot_id} 零分母必须返回null`);
  else check(Math.abs(snapshot.rate-snapshot.numerator/snapshot.denominator)<1e-9, `${snapshot.snapshot_id} 比率计算错误`);
  if (snapshot.synthetic===true) {
    const watermark=systems.events.find((row)=>row.tenant_id===snapshot.tenant_id&&row.event_id===snapshot.source_watermark&&row.product_id===snapshot.product_id);
    check(!!watermark&&(snapshot.metric_id==='return_passed_item_rate'?watermark.type==='warehouse.return.inspected':watermark.type.startsWith('refund.')), `${snapshot.snapshot_id} 源水位未关联同租户/商品的权威事件`);
  }
  if (snapshot.denominator<snapshot.min_sample) check(snapshot.quality==='insufficient_sample', `${snapshot.snapshot_id} 小样本未触发质量门`);
  if (snapshot.baseline_snapshot_id) {
    const baseline = systems.metric_snapshots.find((row)=>row.snapshot_id===snapshot.baseline_snapshot_id);
    check(!!baseline&&baseline.tenant_id===snapshot.tenant_id&&baseline.product_id===snapshot.product_id&&baseline.metric_id===snapshot.metric_id, `${snapshot.snapshot_id} 基准快照缺失或范围不一致`);
    if (baseline) check(baseline.numerator===snapshot.baseline_numerator&&baseline.denominator===snapshot.baseline_denominator&&baseline.rate===snapshot.baseline_rate, `${snapshot.snapshot_id} 内嵌基准值与基准快照不一致`);
  }
}
for (const snapshot of systems.metric_snapshots.filter((row)=>row.metric_id==='refund_successful_line_rate')) {
  const cohortDetails = systems.payment_details.filter((row)=>row.tenant_id===snapshot.tenant_id&&row.product_id===snapshot.product_id&&row.paid_at>=snapshot.observation_start+'T00:00:00Z'&&row.paid_at<snapshot.observation_end+'T00:00:00Z');
  const refundedIds = new Set(systems.refund_details.filter((row)=>row.tenant_id===snapshot.tenant_id&&row.status==='succeeded').map((row)=>row.payment_detail_id));
  const numerator = cohortDetails.filter((row)=>refundedIds.has(row.payment_detail_id)).length;
  check(cohortDetails.length===snapshot.denominator&&numerator===snapshot.numerator, `${snapshot.snapshot_id} 与权威支付明细/成功退款明细不一致`);
}
for (const snapshot of systems.metric_snapshots.filter((row)=>row.metric_id==='refund_goods_amount_rate')) {
  const cohortDetails = systems.payment_details.filter((row)=>row.tenant_id===snapshot.tenant_id&&row.product_id===snapshot.product_id&&row.paid_at>=snapshot.observation_start+'T00:00:00Z'&&row.paid_at<snapshot.observation_end+'T00:00:00Z');
  const cohortIds = new Set(cohortDetails.map((row)=>row.payment_detail_id));
  const numerator = systems.refund_details.filter((row)=>row.tenant_id===snapshot.tenant_id&&row.status==='succeeded'&&cohortIds.has(row.payment_detail_id)).reduce((sum,row)=>sum+row.refunded_goods_minor,0);
  check(cohortDetails.reduce((sum,row)=>sum+row.paid_goods_minor,0)===snapshot.denominator&&numerator===snapshot.numerator, `${snapshot.snapshot_id} 与权威支付货款/退款事实不一致`);
}
for (const snapshot of systems.metric_snapshots.filter((row)=>row.metric_id==='return_passed_item_rate'&&row.quality==='complete')) {
  const productForOrder = (orderId) => {
    const line = systems.order_lines.find((row)=>row.tenant_id===snapshot.tenant_id&&row.order_id===orderId);
    if (!line) return null;
    if (line.product_id) return line.product_id;
    return systems.product_mappings.find((row)=>row.tenant_id===snapshot.tenant_id&&row.source_system==='OMS'&&row.source_id===line.source_product_id&&row.status==='active')?.canonical_product_id || null;
  };
  const batches = systems.shipment_batches.filter((row)=>row.tenant_id===snapshot.tenant_id&&row.signed_at>=snapshot.observation_start+'T00:00:00Z'&&row.signed_at<snapshot.observation_end+'T00:00:00Z'&&productForOrder(row.order_id)===snapshot.product_id);
  const orderIds = new Set(batches.map((row)=>row.order_id));
  const rmas = systems.return_authorizations.filter((row)=>row.tenant_id===snapshot.tenant_id&&orderIds.has(row.order_id));
  const receiptIds = new Set(systems.warehouse_receipts.filter((row)=>row.tenant_id===snapshot.tenant_id&&rmas.some((rma)=>rma.rma_id===row.rma_id)).map((row)=>row.receipt_id));
  const accepted = systems.warehouse_inspections.filter((row)=>row.tenant_id===snapshot.tenant_id&&receiptIds.has(row.receipt_id)).reduce((sum,row)=>sum+row.passed_quantity,0);
  check(batches.reduce((sum,row)=>sum+row.quantity,0)===snapshot.denominator&&accepted===snapshot.numerator, `${snapshot.snapshot_id} 与权威签收批次/验收通过量不一致`);
}
const authoritativeRecords = new Set([
  ...systems.orders.map((row)=>row.order_id),
  ...systems.warehouse_receipts.map((row)=>row.source_record_id),
  ...systems.refund_executions.map((row)=>row.source_record_id),
  ...systems.metric_snapshots.map((row)=>row.snapshot_id)
]);
for (const item of systems.anomaly_evidence) check(authoritativeRecords.has(item.source_record_id), `${item.evidence_id} 未引用权威源记录`);

const traceCsv = fs.readFileSync(path.join(dataRoot,'scenarios/v2/traceability.csv'),'utf8').trim().split(/\r?\n/).slice(1);
const traceRows = traceCsv.map((line)=>line.split(','));
check(traceRows.length>=scenarios.length, '追溯表记录少于场景数');
for (const scenario of scenarios) for (const fr of scenario.requirement_ids) check(traceRows.some((row)=>row[0]===fr&&row[1]===scenario.tc_id&&row[2]===scenario.case_id&&row[3]===manifest.dataset_version), `${scenario.case_id} 缺少FR-TC-case追溯行`);

const acceptanceSource=fs.readFileSync(path.join(projectRoot,'docs/需求详细规格.md'),'utf8');
const requiredAcceptanceTcs=[...new Set([...acceptanceSource.matchAll(/^\|\s*(TC-\d+[A-Z]?)\s*\|/gm)].map((match)=>match[1]))];
const acceptanceCriteriaByTc=new Map([...acceptanceSource.matchAll(/^\|\s*(TC-\d+[A-Z]?)\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|\s*$/gm)].map((match)=>[match[1],{input:match[2],criteria:match[3]}]));
const acceptanceMapText=fs.readFileSync(path.join(dataRoot,'scenarios/v2/acceptance-test-map.csv'),'utf8').trim().split(/\r?\n/);
const acceptanceMapRows=acceptanceMapText.slice(1).map((line)=>line.split(','));
const acceptanceMapByTc=new Map(acceptanceMapRows.map((row)=>[row[0],row]));
check(acceptanceMapByTc.size===requiredAcceptanceTcs.length&&requiredAcceptanceTcs.every((tc)=>acceptanceMapByTc.has(tc)), 'TC验收台账未完整覆盖需求详细规格中的全部验收编号');
for (const tc of requiredAcceptanceTcs) {
  const row=acceptanceMapByTc.get(tc);
  const actualCases=scenarios.filter((scenario)=>scenario.tc_id===tc).map((scenario)=>scenario.case_id).sort().join('|');
  check(!!actualCases&&row?.[1]==='scenario_defined'&&row?.[2]===actualCases&&row?.[3]===`AT-${tc}`&&row?.[4].startsWith('tests/acceptance/test_')&&row?.[5]==='not_implemented', `${tc} 场景/计划测试映射不完整，或错误宣称运行测试已完成`);
  const source=acceptanceCriteriaByTc.get(tc);
  if (source) for (const scenario of scenarios.filter((item)=>item.tc_id===tc&&item.expected.acceptance_criteria)) {
    check(scenario.input.prompt===source.input&&scenario.expected.acceptance_criteria.includes(source.criteria), `${scenario.case_id} 测试输入或验收标准与需求规格不一致`);
  }
}
for (const tc of ['TC-06','TC-07',...Array.from({length:9},(_,i)=>`TC-${69+i}`),...Array.from({length:8},(_,i)=>`TC-${87+i}`)]) check(scenarios.some((scenario)=>scenario.tc_id===tc), `关键退款/工作台验收场景缺失: ${tc}`);
check(manifest.counts.acceptance_tcs===requiredAcceptanceTcs.length&&requiredAcceptanceTcs.length===131, 'manifest验收用例数量与正式需求不一致');

for (const file of manifest.files) {
  const absolute = path.resolve(projectRoot,file.path);
  check(absolute.startsWith(projectRoot+path.sep)&&fs.existsSync(absolute), `manifest文件缺失或越界: ${file.path}`);
  if (fs.existsSync(absolute)) {
    check(hash(absolute)===file.sha256, `${file.path} SHA-256不符`);
    check(fs.statSync(absolute).size===file.bytes, `${file.path} 文件大小不符`);
  }
}

const report = { result: errors.length ? 'failed' : 'passed', scope: 'offline_mock_v2_dataset_validation_only', dataset_version: manifest.dataset_version, reference_time: manifest.reference_time, checks, counts: { synthetic_order_bundles: systems.synthetic_batch.generated_orders, business_records: currentBusinessRecordTotal, business_record_counts: currentBusinessRecordCounts, scenarios: scenarios.length, requirements: new Set(scenarios.flatMap((row)=>row.requirement_ids)).size, evidence: systems.anomaly_evidence.length + knowledgeCorpus.evidence.length, knowledge_documents: knowledgeCorpus.documents.length, graph_edges: knowledgeCorpus.graph.edges.length, errors: errors.length }, errors };
const reportPath = path.join(dataRoot,'generated/reports/mock-v2-data-validation.json');
fs.mkdirSync(path.dirname(reportPath),{recursive:true});
fs.writeFileSync(reportPath,JSON.stringify(report,null,2)+'\n','utf8');
console.log(JSON.stringify(report));
if (errors.length) process.exitCode=1;
