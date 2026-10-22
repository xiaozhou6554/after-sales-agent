// 从现有知识原文生成发布候选清单；仅 --write 会写入新清单，原文不变。
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..', '..');
const sourceFiles = [
  'data/knowledge/v2/corpus.json',
  'data/knowledge/v2/evidence.json',
  'data/knowledge/v2/ontology/core.ttl',
  'data/knowledge/v2/ontology/constraints.shacl.ttl',
  'data/knowledge/v2/ontology/source_mappings.json',
  'data/scenarios/v2/knowledge_labels.jsonl',
];
const digest = value => crypto.createHash('sha256').update(value).digest('hex');
const read = file => fs.readFileSync(path.join(root, file));
const corpus = JSON.parse(read(sourceFiles[0]).toString('utf8'));
const externalEvidence = JSON.parse(read(sourceFiles[1]).toString('utf8'));
const labels = read(sourceFiles[5]).toString('utf8').trim().split(/\r?\n/).map(JSON.parse);
const fail = message => { throw new Error(message); };

if (corpus.dataset_version !== 'mock-v2' || !corpus.synthetic_only) fail('知识源版本错误');
const documents = new Map(corpus.documents.map(item => [`${item.tenant_id}:${item.document_id}`, item]));
const evidence = new Map(corpus.evidence.map(item => [`${item.tenant_id}:${item.evidence_id}`, item]));
const nodes = new Map(corpus.graph.nodes.map(item => [`${item.tenant_id}:${item.node_id}`, item]));
const edges = new Map(corpus.graph.edges.map(item => [`${item.tenant_id}:${item.edge_id}`, item]));
if (documents.size !== corpus.documents.length || evidence.size !== corpus.evidence.length ||
    nodes.size !== corpus.graph.nodes.length || edges.size !== corpus.graph.edges.length) fail('知识标识重复');
const requiredNodeFields = {
  Product: ['product_id', 'name', 'model', 'status', 'version'],
  Accessory: ['accessory_id', 'name', 'model', 'status', 'version'],
  FaultSymptom: ['fault_id', 'name', 'description', 'severity', 'version'],
  ServiceItem: ['service_id', 'name', 'description', 'version'],
  ProductCategory: ['category_id', 'name', 'version'],
  Policy: ['policy_id', 'name', 'policy_type', 'status', 'version', 'effective_from', 'effective_to'],
};
for (const item of corpus.graph.nodes) {
  const required = requiredNodeFields[item.kind];
  if (!required || required.some(field => item[field] === undefined || item[field] === null || item[field] === '')) {
    fail(`图谱节点类型或必填属性无效: ${item.node_id}`);
  }
}
for (const item of corpus.documents) {
  const ref = evidence.get(`${item.tenant_id}:${item.evidence_id}`);
  if (!ref || ref.document_id !== item.document_id || !item.content.trim() ||
      ref.source_record_id !== item.document_id || ref.source_version !== `${item.document_id}:v${item.version}` ||
      ref.status !== item.status || ref.valid_from !== item.valid_from || ref.valid_to !== item.valid_to) {
    fail(`文档证据或版本无效: ${item.document_id}`);
  }
}
const relationKinds = new Map([
  ['BELONGS_TO', ['Product', 'ProductCategory']],
  ['HAS_VERSION', ['Product', 'ProductVersion']],
  ['HAS_ACCESSORY', ['Product', 'Accessory']],
  ['COMPATIBLE_WITH', ['Product', 'Accessory']],
  ['NOT_COMPATIBLE_WITH', ['Product', 'Accessory']],
  ['HAS_FEATURE', ['Product', 'Feature']],
  ['SUITABLE_FOR', ['Product', 'UsageScenario']],
  ['HAS_FAULT_SYMPTOM', ['Product', 'FaultSymptom']],
  ['SUGGESTS_SERVICE', ['FaultSymptom', 'ServiceItem']],
  ['HAS_POLICY', ['Product', 'Policy']],
  ['APPLIES_TO', ['Policy', 'ProductCategory']],
  ['CONTAINS_CLAUSE', ['Policy', 'PolicyClause']],
  ['HAS_WARRANTY', ['Product', 'WarrantyRule']],
  ['SUPPORTED_BY', ['PolicyClause', 'KnowledgeDocument']],
  ['DESCRIBES', ['KnowledgeDocument', 'Product']],
  ['RELATED_TO', ['KnowledgeDocument', 'Policy']],
]);
const date = value => /^\d{4}-\d{2}-\d{2}$/.test(value) && Number.isFinite(Date.parse(value));
for (const item of corpus.graph.edges) {
  const prefix = `${item.tenant_id}:`;
  const from = nodes.get(prefix + item.from);
  const to = nodes.get(prefix + item.to);
  const document = documents.get(prefix + item.document_id);
  const ref = evidence.get(prefix + item.evidence_id);
  const kinds = relationKinds.get(item.relation);
  if (!from || !to || !document || !ref || ref.document_id !== item.document_id ||
      !kinds || from.kind !== kinds[0] || to.kind !== kinds[1] ||
      item.version !== ref.source_version || item.valid_from !== ref.valid_from ||
      item.valid_to !== (ref.valid_to || '9999-12-31') ||
      !date(item.valid_from) || !date(item.valid_to) || item.valid_from >= item.valid_to ||
      document.status !== 'published') {
    fail(`图谱关系类型、证据或版本无效: ${item.edge_id}`);
  }
}
const compatibility = new Set(corpus.graph.edges.filter(item => item.relation === 'COMPATIBLE_WITH')
  .map(item => `${item.tenant_id}:${item.from}:${item.to}`));
if (corpus.graph.edges.some(item => item.relation === 'NOT_COMPATIBLE_WITH' &&
    compatibility.has(`${item.tenant_id}:${item.from}:${item.to}`))) fail('兼容与不兼容关系同时有效');
if (!Array.isArray(externalEvidence) || !externalEvidence.every(item => item.tenant_id && item.evidence_id)) {
  fail('跨系统来源证据格式错误');
}
for (const item of labels) {
  const prefix = `${item.tenant_id}:`;
  if (!['development', 'holdout'].includes(item.split)) fail(`标注集划分无效: ${item.case_id}`);
  for (const id of item.document_ids) if (!documents.has(prefix + id)) fail(`标注文档无效: ${id}`);
  for (const id of item.evidence_ids) if (!evidence.has(prefix + id)) fail(`标注证据无效: ${id}`);
  for (const id of item.edge_ids) if (!edges.has(prefix + id)) fail(`标注关系无效: ${id}`);
}
if (new Set(labels.map(item => item.case_id)).size !== labels.length) fail('知识标注编号重复');
const forbidden = /\b(?:ORDER|PAY|MOCK-TRACK)-\d+\b/i;
if (corpus.documents.some(item => forbidden.test(item.content))) fail('知识原文含动态交易标识');
const manifest = {
  schema_version: '1.0',
  dataset_version: corpus.dataset_version,
  knowledge_release_id: corpus.release_id,
  status: 'draft',
  synthetic_only: true,
  documents: corpus.documents.map(item => ({
    tenant_id: item.tenant_id,
    document_id: item.document_id,
    chunk_id: `${item.document_id}:v${item.version}:0001`,
    evidence_id: item.evidence_id,
    version: item.version,
    status: item.status,
    content_sha256: digest(Buffer.from(item.content, 'utf8')),
  })),
  graph: { nodes: corpus.graph.nodes.length, edges: corpus.graph.edges.length },
  labels: { development: labels.filter(item => item.split === 'development').length,
            holdout: labels.filter(item => item.split === 'holdout').length },
  files: Object.fromEntries(sourceFiles.map(file => [file, digest(read(file))])),
};
const target = path.join(root, 'data/manifests/knowledge-v2.json');
if (process.argv.includes('--write')) {
  fs.writeFileSync(target, JSON.stringify(manifest, null, 2) + '\n', 'utf8');
  console.log(`知识发布候选清单已生成: ${target}`);
} else {
  const existing = JSON.parse(fs.readFileSync(target, 'utf8'));
  if (JSON.stringify(existing) !== JSON.stringify(manifest)) fail('知识发布候选清单与原文不一致');
  console.log(`知识发布候选清单通过: ${manifest.documents.length}篇文档、${manifest.graph.edges}条关系、${labels.length}条标注`);
}
