// 按正式需求的指定 TC 更新设计场景，并重算已登记契约/场景的清单哈希。
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');

const root = path.resolve(__dirname, '../..');
const tc = process.argv[2];
if (!/^TC-\d+[A-Z]?$/.test(tc || '')) {
  throw new Error('须显式提供需同步的 TC 编号');
}
const source = fs.readFileSync(path.join(root, 'docs/需求详细规格.md'), 'utf8');
const row = [...source.matchAll(/^\|\s*(TC-\d+[A-Z]?)\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|\s*$/gm)]
  .find((match) => match[1] === tc);
if (!row) throw new Error(`${tc} 不在需求详细规格验收表中`);

const scenarioPath = path.join(root, 'data/scenarios/v2/multi_system.jsonl');
const original = fs.readFileSync(scenarioPath, 'utf8');
let changed = 0;
const lines = original.trimEnd().split(/\r?\n/).map((line) => {
  const scenario = JSON.parse(line);
  if (scenario.tc_id !== tc) return line;
  if (!Array.isArray(scenario.expected.acceptance_criteria)) {
    throw new Error(`${scenario.case_id} 不含可同步验收标准`);
  }
  scenario.input.prompt = row[2];
  scenario.expected.acceptance_criteria = [row[3]];
  changed += 1;
  return JSON.stringify(scenario);
});
if (changed !== 1) throw new Error(`${tc} 应恰好对应一条设计场景，实际 ${changed} 条`);
const refreshed = `${lines.join('\n')}\n`;
if (refreshed !== original) fs.writeFileSync(scenarioPath, refreshed, 'utf8');

const manifestPath = path.join(root, 'data/manifests/mock-v2.json');
const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
for (const relative of ['data/scenarios/v2/multi_system.jsonl', 'data/contracts/openapi.yaml']) {
  const entry = manifest.files.find((item) => item.path === relative);
  if (!entry) throw new Error(`清单缺少 ${relative}`);
  const bytes = fs.readFileSync(path.join(root, relative));
  entry.sha256 = crypto.createHash('sha256').update(bytes).digest('hex');
  entry.bytes = bytes.length;
}
fs.writeFileSync(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`, 'utf8');
console.log(`${tc} 场景已与需求同步；已刷新场景和 OpenAPI 清单哈希`);
