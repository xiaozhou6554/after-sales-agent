// 校验新增只读物流事实与现有 OMS 签收批次的一致性。
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..', '..');
const logisticsPath = 'data/mock/v2/logistics.json';
const systemsPath = 'data/mock/v2/systems.json';
const manifestPath = path.join(root, 'data/manifests/logistics-v2.json');
const read = file => fs.readFileSync(path.join(root, file));
const digest = file => crypto.createHash('sha256').update(read(file)).digest('hex');
const logistics = JSON.parse(read(logisticsPath));
const systems = JSON.parse(read(systemsPath));
if (logistics.dataset_version !== systems.dataset_version || !logistics.synthetic_only) {
  throw new Error('物流版本或来源无效');
}
const orders = new Set(systems.orders.map(item => `${item.tenant_id}:${item.order_id}`));
const tracking = new Set();
for (const item of logistics.shipments) {
  const key = `${item.tenant_id}:${item.tracking_number}`;
  if (!orders.has(`${item.tenant_id}:${item.order_id}`) || tracking.has(key)) {
    throw new Error(`运单归属或唯一性无效: ${key}`);
  }
  tracking.add(key);
  const events = item.events.map(event => new Date(event.time).getTime());
  if (!events.length || events.some((time, index) => !Number.isFinite(time) ||
      (index > 0 && time < events[index - 1]))) throw new Error(`轨迹时间无效: ${key}`);
  const signed = systems.shipment_batches.filter(batch => batch.tenant_id === item.tenant_id &&
    batch.order_id === item.order_id).map(batch => batch.signed_at);
  if (item.status === 'delivered' && !signed.some(time => events.includes(new Date(time).getTime()))) {
    throw new Error(`签收时间与 OMS 批次不符: ${key}`);
  }
  if (item.estimated_delivery_at && new Date(item.estimated_delivery_at).getTime() <= 0) {
    throw new Error(`预计到达时间无效: ${key}`);
  }
}
const manifest = {
  schema_version: '1.0', dataset_version: logistics.dataset_version,
  reference_time: logistics.reference_time, synthetic_only: true,
  shipment_count: logistics.shipments.length,
  files: { [logisticsPath]: digest(logisticsPath), [systemsPath]: digest(systemsPath) },
};
if (process.argv.includes('--write')) {
  fs.writeFileSync(manifestPath, JSON.stringify(manifest, null, 2) + '\n');
  console.log(`物流清单已生成: ${logistics.shipments.length} 条`);
} else if (JSON.stringify(JSON.parse(fs.readFileSync(manifestPath))) !== JSON.stringify(manifest)) {
  throw new Error('物流清单与源事实不一致');
} else console.log(`物流事实校验通过: ${logistics.shipments.length} 条`);
