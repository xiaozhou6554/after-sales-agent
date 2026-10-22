// Deterministically rebuild isolated synthetic v2 fixtures. Never connects to business systems.
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');

const dataRoot = path.resolve(__dirname, '..');
const projectRoot = path.resolve(dataRoot, '..');
const version = 'mock-v2';
const referenceTime = '2026-10-19T04:00:00.000Z';
const write = (relativePath, value) => {
  const destination = path.resolve(dataRoot, relativePath);
  if (!destination.startsWith(dataRoot + path.sep)) throw new Error(`Refusing path outside data/: ${relativePath}`);
  fs.mkdirSync(path.dirname(destination), { recursive: true });
  fs.writeFileSync(destination, typeof value === 'string' ? value : JSON.stringify(value, null, 2) + '\n', 'utf8');
};
const resetOwned = (relativePath) => {
  const destination = path.resolve(dataRoot, relativePath);
  if (!destination.startsWith(dataRoot + path.sep)) throw new Error(`Refusing cleanup outside data/: ${relativePath}`);
  fs.rmSync(destination, { recursive: true, force: true });
};

// The user approved discarding the old generated baseline. Preserve schemas and scripts.
for (const oldPath of ['mock/v1', 'knowledge/v1', 'scenarios/v1', 'scenarios/traceability-v2.csv', 'generated/reports/mock-v1-data-validation.json', 'manifests/mock-v1.json']) resetOwned(oldPath);
// 仅清理本脚本拥有的文件，保留后续步骤增加的物流事实和知识标注。
for (const v2Path of [
  'mock/v2/systems.json',
  'knowledge/v2/corpus.json',
  'knowledge/v2/evidence.json',
  'knowledge/v2/ontology/core.ttl',
  'knowledge/v2/ontology/constraints.shacl.ttl',
  'knowledge/v2/ontology/source_mappings.json',
  'knowledge/v2/ontology/metrics.json',
  'scenarios/v2/multi_system.jsonl',
  'scenarios/v2/catalog.json',
  'scenarios/v2/traceability.csv',
  'scenarios/v2/acceptance-test-map.csv',
  'generated/reports/mock-v2-data-validation.json',
  'manifests/mock-v2.json',
]) resetOwned(v2Path);

const systems = {
  schema_version: '2.0', dataset_version: version, reference_time: referenceTime, synthetic_only: true,
  tenants: [
    { tenant_id: 'tenant-north', synthetic: true },
    { tenant_id: 'tenant-south', synthetic: true }
  ],
  products: [
    { canonical_product_id: 'product-air-01', tenant_id: 'tenant-north', name: '模拟净化器 A', active: true },
    { canonical_product_id: 'product-air-02', tenant_id: 'tenant-north', name: '模拟净化器 B', active: true },
    { canonical_product_id: 'product-air-91', tenant_id: 'tenant-south', name: '模拟净化器 A', active: true }
  ],
  product_mappings: [
    { tenant_id: 'tenant-north', source_system: 'PIM', source_id: 'P-100', canonical_product_id: 'product-air-01', version: 2, valid_from: '2026-01-01', valid_to: null, status: 'active' },
    { tenant_id: 'tenant-north', source_system: 'OMS', source_id: 'SKU-100', canonical_product_id: 'product-air-01', version: 2, valid_from: '2026-01-01', valid_to: null, status: 'active' },
    { tenant_id: 'tenant-north', source_system: 'WMS', source_id: 'ITEM-100', canonical_product_id: 'product-air-01', version: 2, valid_from: '2026-01-01', valid_to: null, status: 'active' },
    { tenant_id: 'tenant-north', source_system: 'OMS', source_id: 'AMB-7', canonical_product_id: 'product-air-01', version: 1, valid_from: '2026-01-01', valid_to: null, status: 'ambiguous' },
    { tenant_id: 'tenant-north', source_system: 'OMS', source_id: 'AMB-7', canonical_product_id: 'product-air-02', version: 1, valid_from: '2026-01-01', valid_to: null, status: 'ambiguous' },
    { tenant_id: 'tenant-south', source_system: 'OMS', source_id: 'SKU-100', canonical_product_id: 'product-air-91', version: 1, valid_from: '2026-01-01', valid_to: null, status: 'active' }
  ],
  orders: [
    { tenant_id: 'tenant-north', order_id: 'ORDER-001', customer_id: 'CUSTOMER-01', status: 'delivered', currency: 'CNY', paid_minor: 100000, version: 3 },
    { tenant_id: 'tenant-north', order_id: 'ORDER-002', customer_id: 'CUSTOMER-02', status: 'delivered', currency: 'CNY', paid_minor: 50000, version: 2 },
    { tenant_id: 'tenant-north', order_id: 'ORDER-003', customer_id: 'CUSTOMER-03', status: 'delivered', currency: 'CNY', paid_minor: 10000000, version: 1 },
    { tenant_id: 'tenant-north', order_id: 'ORDER-005', customer_id: 'CUSTOMER-05', status: 'delivered', currency: 'CNY', paid_minor: 10000000, version: 1 },
    { tenant_id: 'tenant-south', order_id: 'ORDER-001', customer_id: 'CUSTOMER-91', status: 'delivered', currency: 'CNY', paid_minor: 100000, version: 1 }
  ],
  order_lines: [
    { tenant_id: 'tenant-north', order_id: 'ORDER-001', line_id: 'LINE-001', source_product_id: 'SKU-100', quantity: 100, goods_minor: 90000, shipping_minor: 10000 },
    { tenant_id: 'tenant-north', order_id: 'ORDER-002', line_id: 'LINE-002', source_product_id: 'SKU-100', quantity: 50, goods_minor: 45000, shipping_minor: 5000 },
    { tenant_id: 'tenant-north', order_id: 'ORDER-003', line_id: 'LINE-003', source_product_id: 'SKU-100', quantity: 100, goods_minor: 9000000, shipping_minor: 1000000 },
    { tenant_id: 'tenant-north', order_id: 'ORDER-005', line_id: 'LINE-005', source_product_id: 'SKU-100', quantity: 100, goods_minor: 9000000, shipping_minor: 1000000 },
    { tenant_id: 'tenant-south', order_id: 'ORDER-001', line_id: 'LINE-091', source_product_id: 'SKU-100', quantity: 100, goods_minor: 90000, shipping_minor: 10000 }
  ],
  shipment_batches: [
    { tenant_id: 'tenant-north', order_id: 'ORDER-001', batch_id: 'SHIP-BATCH-01', quantity: 100, signed_at: '2026-09-01T08:00:00Z' },
    { tenant_id: 'tenant-north', order_id: 'ORDER-005', batch_id: 'SHIP-BATCH-BASE', quantity: 100, signed_at: '2026-08-01T08:00:00Z' },
    { tenant_id: 'tenant-north', order_id: 'ORDER-002', batch_id: 'SHIP-BATCH-02', quantity: 50, signed_at: '2026-10-01T08:00:00Z' }
  ],
  return_authorizations: [
    { tenant_id: 'tenant-north', rma_id: 'RMA-001', order_id: 'ORDER-001', line_id: 'LINE-001', authorized_quantity: 15, status: 'authorized' },
    { tenant_id: 'tenant-north', rma_id: 'RMA-BASE', order_id: 'ORDER-005', line_id: 'LINE-005', authorized_quantity: 5, status: 'authorized' },
    { tenant_id: 'tenant-north', rma_id: 'RMA-002', order_id: 'ORDER-002', line_id: 'LINE-002', authorized_quantity: 5, status: 'authorized' }
  ],
  warehouse_receipts: [
    { tenant_id: 'tenant-north', receipt_id: 'RECEIPT-001', rma_id: 'RMA-001', received_quantity: 14, inspected_quantity: 14, source_record_id: 'WMS-REC-001' },
    { tenant_id: 'tenant-north', receipt_id: 'RECEIPT-BASE', rma_id: 'RMA-BASE', received_quantity: 4, inspected_quantity: 4, source_record_id: 'WMS-REC-BASE' }
  ],
  warehouse_inspections: [
    { tenant_id: 'tenant-north', inspection_id: 'INSPECT-001', receipt_id: 'RECEIPT-001', passed_quantity: 10, rejected_quantity: 4, reason_code: 'SEAL_DEFECT', reason_version: 1, confirmed_by: 'warehouse-user-01' },
    { tenant_id: 'tenant-north', inspection_id: 'INSPECT-BASE', receipt_id: 'RECEIPT-BASE', passed_quantity: 4, rejected_quantity: 0, reason_code: 'SEAL_DEFECT', reason_version: 1, confirmed_by: 'warehouse-user-01' }
  ],
  payments: [
    { tenant_id: 'tenant-north', payment_id: 'PAY-001', order_id: 'ORDER-001', paid_minor: 100000, currency: 'CNY', status: 'succeeded', source_record_id: 'PSP-PAY-001' },
    { tenant_id: 'tenant-north', payment_id: 'PAY-002', order_id: 'ORDER-002', paid_minor: 50000, currency: 'CNY', status: 'succeeded', source_record_id: 'PSP-PAY-002' },
    { tenant_id: 'tenant-north', payment_id: 'PAY-BATCH-100', order_id: 'ORDER-003', paid_minor: 10000000, currency: 'CNY', status: 'succeeded', source_record_id: 'PSP-PAY-100' },
    { tenant_id: 'tenant-north', payment_id: 'PAY-BATCH-BASE', order_id: 'ORDER-005', paid_minor: 10000000, currency: 'CNY', status: 'succeeded', source_record_id: 'PSP-PAY-BASE' }
  ],
  payment_details: Array.from({length:200}, (_,index)=>{ const baseline=index>=100; const n=baseline?index-99:index+1; return { tenant_id:'tenant-north', payment_detail_id:`PAY-DETAIL-${String(index+1).padStart(3,'0')}`, payment_id:baseline?'PAY-BATCH-BASE':'PAY-BATCH-100', order_id:baseline?'ORDER-005':'ORDER-003', line_id:baseline?'LINE-005':'LINE-003', product_id:'product-air-01', paid_goods_minor:90000, paid_shipping_minor:10000, paid_minor:100000, paid_at:baseline?'2026-08-15T08:00:00Z':'2026-09-15T08:00:00Z', detail_sequence:n }; }),
  refund_details: Array.from({length:8}, (_,index)=>{ const detailNumber=index>=5?index+96:index+1; return { tenant_id:'tenant-north', refund_detail_id:`REFUND-DETAIL-${String(index+1).padStart(3,'0')}`, payment_detail_id:`PAY-DETAIL-${String(detailNumber).padStart(3,'0')}`, refund_id:`REFUND-BATCH-${String(index+1).padStart(3,'0')}`, status:'succeeded', refunded_goods_minor:90000, currency:'CNY', source_record_id:`PSP-REF-BATCH-${String(index+1).padStart(3,'0')}` }; }),
  refund_executions: [
    { tenant_id: 'tenant-north', refund_id: 'REFUND-001', order_id: 'ORDER-001', payment_id: 'PAY-001', amount_minor: 10000, currency: 'CNY', status: 'succeeded', idempotency_key: 'idem-refund-001', source_record_id: 'PSP-REF-001' },
    ...Array.from({length:8}, (_,index)=>{ const baseline=index>=5; return { tenant_id:'tenant-north', refund_id:`REFUND-BATCH-${String(index+1).padStart(3,'0')}`, order_id:baseline?'ORDER-005':'ORDER-003', payment_id:baseline?'PAY-BATCH-BASE':'PAY-BATCH-100', amount_minor:90000, currency:'CNY', status:'succeeded', idempotency_key:`idem-refund-batch-${index+1}`, source_record_id:`PSP-REF-BATCH-${String(index+1).padStart(3,'0')}` }; })
  ],
  metric_snapshots: [
    { tenant_id: 'tenant-north', snapshot_id: 'METRIC-RETURN-2026-08', metric_id: 'return_passed_item_rate', metric_version: 1, product_id: 'product-air-01', cohort: 'signed-2026-08', observation_start: '2026-08-01', observation_end: '2026-09-01', numerator: 4, denominator: 100, rate: 0.04, min_sample: 100, source_watermark: 'WMS-EVT-0800', quality: 'complete' },
    { tenant_id: 'tenant-north', snapshot_id: 'METRIC-REFUND-LINE-2026-08', metric_id: 'refund_successful_line_rate', metric_version: 1, product_id: 'product-air-01', cohort: 'paid-2026-08', observation_start: '2026-08-01', observation_end: '2026-09-01', numerator: 3, denominator: 100, rate: 0.03, min_sample: 100, source_watermark: 'PAY-EVT-0800', quality: 'complete' },
    { tenant_id: 'tenant-north', snapshot_id: 'METRIC-REFUND-AMOUNT-2026-08', metric_id: 'refund_goods_amount_rate', metric_version: 1, product_id: 'product-air-01', cohort: 'paid-2026-08', observation_start: '2026-08-01', observation_end: '2026-09-01', numerator: 270000, denominator: 9000000, rate: 0.03, min_sample: 100, source_watermark: 'PAY-EVT-0800', quality: 'complete', unit: 'CNY_minor' },
    { tenant_id: 'tenant-north', snapshot_id: 'METRIC-RETURN-2026-09', metric_id: 'return_passed_item_rate', metric_version: 1, product_id: 'product-air-01', cohort: 'signed-2026-09', observation_start: '2026-09-01', observation_end: '2026-10-01', numerator: 10, denominator: 100, rate: 0.1, baseline_numerator: 4, baseline_denominator: 100, baseline_rate: 0.04, baseline_snapshot_id: 'METRIC-RETURN-2026-08', min_sample: 100, source_watermark: 'WMS-EVT-0900', quality: 'complete' },
    { tenant_id: 'tenant-north', snapshot_id: 'METRIC-REFUND-LINE-2026-09', metric_id: 'refund_successful_line_rate', metric_version: 1, product_id: 'product-air-01', cohort: 'paid-2026-09', observation_start: '2026-09-01', observation_end: '2026-10-01', numerator: 5, denominator: 100, rate: 0.05, baseline_numerator: 3, baseline_denominator: 100, baseline_rate: 0.03, baseline_snapshot_id: 'METRIC-REFUND-LINE-2026-08', min_sample: 100, source_watermark: 'PAY-EVT-0900', quality: 'complete' },
    { tenant_id: 'tenant-north', snapshot_id: 'METRIC-REFUND-AMOUNT-2026-09', metric_id: 'refund_goods_amount_rate', metric_version: 1, product_id: 'product-air-01', cohort: 'paid-2026-09', observation_start: '2026-09-01', observation_end: '2026-10-01', numerator: 450000, denominator: 9000000, rate: 0.05, baseline_numerator: 270000, baseline_denominator: 9000000, baseline_rate: 0.03, baseline_snapshot_id: 'METRIC-REFUND-AMOUNT-2026-08', min_sample: 100, source_watermark: 'PAY-EVT-0900', quality: 'complete', unit: 'CNY_minor' },
    { tenant_id: 'tenant-north', snapshot_id: 'METRIC-2026-10-SMALL', metric_id: 'return_passed_item_rate', metric_version: 1, product_id: 'product-air-02', cohort: 'signed-2026-10', observation_start: '2026-10-01', observation_end: '2026-10-19', denominator: 4, numerator: 2, rate: 0.5, baseline_numerator: 0, baseline_denominator: 0, baseline_rate: null, min_sample: 100, source_watermark: 'WMS-EVT-1010', quality: 'insufficient_sample' }
  ],
  events: [
    { tenant_id: 'tenant-north', event_id: 'WMS-EVT-0800', type: 'warehouse.return.inspected', aggregate_id: 'RMA-BASE', version: 1, status: 'processed' },
    { tenant_id: 'tenant-north', event_id: 'WMS-EVT-0900', type: 'warehouse.return.inspected', aggregate_id: 'RMA-001', version: 1, status: 'processed' },
    { tenant_id: 'tenant-north', event_id: 'WMS-EVT-1010', type: 'warehouse.return.inspected', aggregate_id: 'RMA-002', version: 1, status: 'processed' },
    { tenant_id: 'tenant-north', event_id: 'PAY-EVT-001', type: 'refund.execution.succeeded', aggregate_id: 'REFUND-001', version: 1, status: 'processed' },
    { tenant_id: 'tenant-north', event_id: 'PAY-EVT-0800', type: 'refund.execution.succeeded', aggregate_id: 'paid-2026-08', version: 1, status: 'processed' },
    { tenant_id: 'tenant-north', event_id: 'PAY-EVT-0900', type: 'refund.execution.succeeded', aggregate_id: 'paid-2026-09', version: 1, status: 'processed' }
  ],
  traces: [
    { tenant_id: 'tenant-north', trace_id: 'TRACE-RETURN-001', spans: ['api.request', 'analytics.query', 'wms.projection'], status: 'ok' },
    { tenant_id: 'tenant-north', trace_id: 'TRACE-PAYMENT-001', spans: ['api.request', 'payment.execute', 'payment.query'], status: 'timeout_then_reconciled' }
  ],
  anomaly_evidence: [
    { tenant_id: 'tenant-north', evidence_id: 'EVID-RETURN-BASE-001', source_system: 'WMS', source_record_id: 'WMS-REC-BASE', trace_id: 'TRACE-RETURN-001', event_id: 'WMS-EVT-0800', snapshot_id: 'METRIC-RETURN-2026-08', kind: 'baseline_fact', content: '基准期商品 product-air-01 签收100件，质检通过退货4件。' },
    { tenant_id: 'tenant-north', evidence_id: 'EVID-RETURN-001', source_system: 'WMS', source_record_id: 'WMS-REC-001', trace_id: 'TRACE-RETURN-001', event_id: 'WMS-EVT-0900', snapshot_id: 'METRIC-RETURN-2026-09', kind: 'business_fact', content: '商品 product-air-01 本期签收100件，验收通过退货10件；基准为4/100。' },
    { tenant_id: 'tenant-north', evidence_id: 'EVID-PAYMENT-001', source_system: 'PAYMENT', source_record_id: 'PSP-REF-001', trace_id: 'TRACE-PAYMENT-001', event_id: 'PAY-EVT-001', snapshot_id: 'METRIC-REFUND-LINE-2026-09', kind: 'source_fact', content: '模拟支付渠道退款流水 REFUND-001 成功，金额10000分。' },
    { tenant_id: 'tenant-north', evidence_id: 'EVID-QUALITY-001', source_system: 'PROJECTION', source_record_id: 'METRIC-2026-10-SMALL', trace_id: 'TRACE-RETURN-001', event_id: 'WMS-EVT-1010', snapshot_id: 'METRIC-2026-10-SMALL', kind: 'quality', content: '样本分母4，低于最小样本100，不得作为已确认异常排名。' },
    { tenant_id: 'tenant-north', evidence_id: 'EVID-REFUND-BASE-001', source_system: 'PAYMENT', source_record_id: 'METRIC-REFUND-LINE-2026-08', trace_id: 'TRACE-PAYMENT-001', event_id: 'PAY-EVT-0800', snapshot_id: 'METRIC-REFUND-LINE-2026-08', kind: 'baseline_fact', content: '基准付款批次成功退款3/100条支付明细，商品退款金额270000/9000000分。' },
    { tenant_id: 'tenant-north', evidence_id: 'EVID-REFUND-001', source_system: 'PAYMENT', source_record_id: 'METRIC-REFUND-LINE-2026-09', trace_id: 'TRACE-PAYMENT-001', event_id: 'PAY-EVT-0900', snapshot_id: 'METRIC-REFUND-LINE-2026-09', kind: 'metric_fact', content: '2026-09付款批次成功退款5/100条支付明细，商品退款金额450000/9000000分。' }
  ]
};

// Add one thousand reproducible, tenant-isolated synthetic order bundles.
// Each bundle links OMS order/line facts to PIM/WMS mappings, shipment, payment,
// optional return receipt/inspection, optional refund execution, events and metrics.
const syntheticProfiles = [
  { productNo: 1, returnChance: 42, refundChance: 22, reason: 'SEAL_DEFECT' },
  { productNo: 2, returnChance: 28, refundChance: 16, reason: 'MISSING_PART' },
  { productNo: 3, returnChance: 14, refundChance: 12, reason: 'DAMAGED_BOX' },
  { productNo: 4, returnChance: 8, refundChance: 8, reason: 'NOT_AS_DESCRIBED' },
  { productNo: 5, returnChance: 5, refundChance: 5, reason: 'CHANGED_MIND' }
];
const tenantProfiles = [
  { tenantId: 'tenant-north', code: 'N' },
  { tenantId: 'tenant-south', code: 'S' }
];
const syntheticProductIds = new Map();
const syntheticOrders = [];
for (const tenant of tenantProfiles) {
  for (const profile of syntheticProfiles) {
    const productId = `product-synth-${tenant.code.toLowerCase()}-${String(profile.productNo).padStart(2,'0')}`;
    const sourceSku = `SKU-SYN-${tenant.code}-${String(profile.productNo).padStart(2,'0')}`;
    syntheticProductIds.set(`${tenant.tenantId}|${productId}`, { ...profile, sourceSku });
    systems.products.push({ canonical_product_id: productId, tenant_id: tenant.tenantId, name: `Synthetic Product ${tenant.code}-${String(profile.productNo).padStart(2,'0')}`, active: true, synthetic: true });
    for (const [sourceSystem, sourceId] of [['PIM',`PIM-${sourceSku}`],['OMS',sourceSku],['WMS',`ITEM-${sourceSku}`]]) {
      systems.product_mappings.push({ tenant_id: tenant.tenantId, source_system: sourceSystem, source_id: sourceId, canonical_product_id: productId, version: 1, valid_from: '2026-01-01', valid_to: null, status: 'active', synthetic: true });
    }
    for (let orderNo = 1; orderNo <= 100; orderNo++) {
      const suffix = `${tenant.code}-${String(profile.productNo).padStart(2,'0')}-${String(orderNo).padStart(3,'0')}`;
      const orderId = `SYN-ORDER-${suffix}`;
      const lineId = `SYN-LINE-${suffix}`;
      const paymentId = `SYN-PAY-${suffix}`;
      const detailId = `SYN-PAY-DETAIL-${suffix}`;
      const quantity = 1 + ((orderNo + profile.productNo) % 3);
      const unitGoodsMinor = 12000 + ((orderNo * 137 + profile.productNo * 997) % 88000);
      const goodsMinor = unitGoodsMinor * quantity;
      const shippingMinor = 800 + ((orderNo * 31 + profile.productNo * 17) % 2200);
      const paidMinor = goodsMinor + shippingMinor;
      const tenantOffset = tenant.code === 'S' ? 23 : 0;
      const salesChannel = ['web','marketplace','retail','partner'][(orderNo + profile.productNo + tenantOffset) % 4];
      const salesRegion = ['east','south','west','north'][(orderNo * 3 + profile.productNo + tenantOffset) % 4];
      const teamId = `team-${tenant.code.toLowerCase()}-${1 + ((orderNo + profile.productNo + tenantOffset) % 3)}`;
      const day = String(1 + ((orderNo - 1) % 25)).padStart(2,'0');
      const paidAt = `2026-09-${day}T08:00:00Z`;
      const signedAt = `2026-09-${String(Math.min(28, Number(day) + 2)).padStart(2,'0')}T12:00:00Z`;
      const shipmentId = `SYN-SHIP-${suffix}`;
      const receiptNo = `SYN-RECEIPT-${suffix}`;
      const rmaId = `SYN-RMA-${suffix}`;
      const inspectionId = `SYN-INSPECT-${suffix}`;
      const paymentEventId = `SYN-EVT-PAY-${suffix}`;
      const orderEventId = `SYN-EVT-SHIP-${suffix}`;
      systems.orders.push({ tenant_id: tenant.tenantId, order_id: orderId, customer_id: `SYN-CUSTOMER-${suffix}`, status: 'delivered', currency: 'CNY', paid_minor: paidMinor, version: 1, source_system: 'OMS', source_record_id: orderId, source_version: 1, created_at: paidAt, sales_channel: salesChannel, sales_region: salesRegion, team_id: teamId, synthetic: true });
      systems.order_lines.push({ tenant_id: tenant.tenantId, order_id: orderId, line_id: lineId, source_product_id: sourceSku, product_id: productId, quantity, goods_minor: goodsMinor, shipping_minor: shippingMinor, source_system: 'OMS', source_record_id: lineId, source_version: 1, synthetic: true });
      systems.shipment_batches.push({ tenant_id: tenant.tenantId, order_id: orderId, batch_id: shipmentId, product_id: productId, quantity, signed_at: signedAt, source_system: 'WMS', source_record_id: shipmentId, source_version: 1, sales_channel: salesChannel, sales_region: salesRegion, team_id: teamId, synthetic: true });
      systems.payments.push({ tenant_id: tenant.tenantId, payment_id: paymentId, order_id: orderId, paid_minor: paidMinor, currency: 'CNY', status: 'succeeded', source_record_id: `SYN-PSP-PAY-${suffix}`, source_system: 'PAYMENT', source_version: 1, paid_at: paidAt, environment: 'mock', synthetic: true });
      systems.payment_details.push({ tenant_id: tenant.tenantId, payment_detail_id: detailId, payment_id: paymentId, order_id: orderId, line_id: lineId, product_id: productId, quantity, paid_goods_minor: goodsMinor, paid_shipping_minor: shippingMinor, paid_minor: paidMinor, paid_at: paidAt, source_system: 'PAYMENT', source_record_id: detailId, source_version: 1, environment: 'mock', sales_channel: salesChannel, sales_region: salesRegion, team_id: teamId, detail_sequence: 1, synthetic: true });
      syntheticOrders.push({ tenant, profile, productId, orderNo, orderId, lineId, paymentId, detailId, quantity, goodsMinor, paidMinor, paidAt, shipmentId, receiptNo, rmaId, inspectionId, signedAt, paymentEventId, orderEventId });

      const returnRoll = (orderNo * 37 + profile.productNo * 13 + tenantOffset) % 100;
      if (returnRoll < profile.returnChance) {
        const authorizedQuantity = 1 + ((orderNo + profile.productNo) % quantity);
        const receivedQuantity = authorizedQuantity;
        const rejectedQuantity = profile.reason === 'SEAL_DEFECT' && orderNo % 3 === 0 ? Math.min(1, receivedQuantity) : 0;
        const passedQuantity = receivedQuantity - rejectedQuantity;
        const receiptSourceId = `SYN-WMS-REC-${suffix}`;
        const inspectionEventId = `SYN-EVT-WMS-${suffix}`;
        systems.return_authorizations.push({ tenant_id: tenant.tenantId, rma_id: rmaId, order_id: orderId, line_id: lineId, authorized_quantity: authorizedQuantity, status: 'authorized', reason_code: profile.reason, synthetic: true });
        systems.warehouse_receipts.push({ tenant_id: tenant.tenantId, receipt_id: receiptNo, rma_id: rmaId, received_quantity: receivedQuantity, inspected_quantity: receivedQuantity, received_at: `${signedAt.slice(0,10)}T16:00:00Z`, source_record_id: receiptSourceId, source_system: 'WMS', source_version: 1, environment: 'mock', synthetic: true });
        systems.warehouse_inspections.push({ tenant_id: tenant.tenantId, inspection_id: inspectionId, receipt_id: receiptNo, passed_quantity: passedQuantity, rejected_quantity: rejectedQuantity, reason_code: profile.reason, reason_version: 1, confirmed_by: `SYN-WMS-USER-${tenant.code}`, source_record_id: inspectionId, source_system: 'WMS', source_version: 1, environment: 'mock', synthetic: true });
        systems.events.push({ tenant_id: tenant.tenantId, event_id: `SYN-EVT-RETURN-${suffix}`, type: 'return.created', aggregate_id: rmaId, product_id: productId, version: 1, status: 'processed', synthetic: true });
        systems.events.push({ tenant_id: tenant.tenantId, event_id: `SYN-EVT-RECEIPT-${suffix}`, type: 'warehouse.return.received', aggregate_id: rmaId, product_id: productId, version: 1, status: 'processed', synthetic: true });
        systems.events.push({ tenant_id: tenant.tenantId, event_id: inspectionEventId, type: 'warehouse.return.inspected', aggregate_id: rmaId, product_id: productId, version: 1, status: 'processed', synthetic: true });
      }

      const refundRoll = (orderNo * 29 + profile.productNo * 11 + tenantOffset * 2) % 100;
      if (refundRoll < profile.refundChance) {
        const resultRoll = orderNo % 20;
        const status = resultRoll < 14 ? 'succeeded' : resultRoll < 16 ? 'failed' : resultRoll < 18 ? 'unknown' : 'pending';
        const refundId = `SYN-REFUND-${suffix}`;
        const sourceRecordId = `SYN-PSP-REF-${suffix}`;
        const refundAmount = Math.max(1, Math.floor(goodsMinor * (50 + (orderNo % 51)) / 100));
        systems.events.push({ tenant_id: tenant.tenantId, event_id: `SYN-EVT-REFUND-CONFIRMED-${suffix}`, type: 'refund.confirmed', aggregate_id: refundId, product_id: productId, version: 1, status: 'processed', synthetic: true });
        systems.refund_executions.push({ tenant_id: tenant.tenantId, refund_id: refundId, order_id: orderId, payment_id: paymentId, amount_minor: refundAmount, currency: 'CNY', status, idempotency_key: `idem-${refundId.toLowerCase()}`, source_record_id: sourceRecordId, source_system: 'PAYMENT', source_version: 1, environment: 'mock', synthetic: true });
        systems.events.push({ tenant_id: tenant.tenantId, event_id: `SYN-EVT-REFUND-REQUESTED-${suffix}`, type: 'refund.execution.requested', aggregate_id: refundId, product_id: productId, version: 1, status: 'processed', synthetic: true });
        if (status === 'succeeded') {
          systems.refund_details.push({ tenant_id: tenant.tenantId, refund_detail_id: `SYN-REFUND-DETAIL-${suffix}`, payment_detail_id: detailId, refund_id: refundId, status: 'succeeded', refunded_goods_minor: refundAmount, currency: 'CNY', source_record_id: sourceRecordId, synthetic: true });
          systems.events.push({ tenant_id: tenant.tenantId, event_id: `SYN-EVT-REFUND-${suffix}`, type: 'refund.execution.succeeded', aggregate_id: refundId, product_id: productId, version: 1, status: 'processed', synthetic: true });
        } else if (status === 'failed') {
          systems.events.push({ tenant_id: tenant.tenantId, event_id: `SYN-EVT-REFUND-FAILED-${suffix}`, type: 'refund.execution.failed', aggregate_id: refundId, product_id: productId, version: 1, status: 'processed', synthetic: true });
        }
      }
    }
  }
}

// Produce exact source-derived September snapshots for all synthetic products.
for (const tenant of tenantProfiles) {
  for (const profile of syntheticProfiles) {
    const productId = `product-synth-${tenant.code.toLowerCase()}-${String(profile.productNo).padStart(2,'0')}`;
    const orders = syntheticOrders.filter((row)=>row.tenant.tenantId===tenant.tenantId&&row.productId===productId);
    const cohortOrderIds = new Set(orders.map((row)=>row.orderId));
    const returnReceipts = new Set(systems.warehouse_receipts.filter((row)=>row.tenant_id===tenant.tenantId&&orders.some((order)=>order.rmaId===row.rma_id)).map((row)=>row.receipt_id));
    const passedQuantity = systems.warehouse_inspections.filter((row)=>row.tenant_id===tenant.tenantId&&returnReceipts.has(row.receipt_id)).reduce((sum,row)=>sum+row.passed_quantity,0);
    const shippedQuantity = systems.shipment_batches.filter((row)=>row.tenant_id===tenant.tenantId&&row.product_id===productId&&row.signed_at>='2026-09-01T00:00:00Z'&&row.signed_at<'2026-10-01T00:00:00Z').reduce((sum,row)=>sum+row.quantity,0);
    const details = systems.payment_details.filter((row)=>row.tenant_id===tenant.tenantId&&row.product_id===productId&&row.paid_at>='2026-09-01T00:00:00Z'&&row.paid_at<'2026-10-01T00:00:00Z');
    const detailIds = new Set(details.map((row)=>row.payment_detail_id));
    const successfulRefunds = systems.refund_details.filter((row)=>row.tenant_id===tenant.tenantId&&row.status==='succeeded'&&detailIds.has(row.payment_detail_id));
    const refundedLineCount = new Set(successfulRefunds.map((row)=>row.payment_detail_id)).size;
    const paidGoodsMinor = details.reduce((sum,row)=>sum+row.paid_goods_minor,0);
    const refundedGoodsMinor = successfulRefunds.reduce((sum,row)=>sum+row.refunded_goods_minor,0);
    const returnEvents = systems.events.filter((row)=>row.tenant_id===tenant.tenantId&&row.product_id===productId&&row.type==='warehouse.return.inspected');
    const paymentEvents = systems.events.filter((row)=>row.tenant_id===tenant.tenantId&&row.product_id===productId&&row.type.startsWith('refund.'));
    const returnWatermark = returnEvents.at(-1)?.event_id;
    const paymentWatermark = paymentEvents.at(-1)?.event_id;
    const prefix = `SYN-METRIC-${tenant.code}-${String(profile.productNo).padStart(2,'0')}`;
    const base = { tenant_id: tenant.tenantId, product_id: productId, metric_version: 1, observation_start: '2026-09-01', observation_end: '2026-10-01', min_sample: 100, quality: 'complete', synthetic: true };
    systems.metric_snapshots.push({ ...base, snapshot_id: `${prefix}-RETURN`, metric_id: 'return_passed_item_rate', cohort: 'signed-2026-09', source_watermark: returnWatermark, numerator: passedQuantity, denominator: shippedQuantity, rate: shippedQuantity ? passedQuantity / shippedQuantity : null, ontology_version: 'v2', mapping_version: 1, as_of: referenceTime, projection_snapshot_id: `${prefix}-RETURN` });
    systems.metric_snapshots.push({ ...base, snapshot_id: `${prefix}-REFUND-LINE`, metric_id: 'refund_successful_line_rate', cohort: 'paid-2026-09', source_watermark: paymentWatermark, numerator: refundedLineCount, denominator: details.length, rate: details.length ? refundedLineCount / details.length : null, ontology_version: 'v2', mapping_version: 1, as_of: referenceTime, projection_snapshot_id: `${prefix}-REFUND-LINE` });
    systems.metric_snapshots.push({ ...base, snapshot_id: `${prefix}-REFUND-AMOUNT`, metric_id: 'refund_goods_amount_rate', cohort: 'paid-2026-09', source_watermark: paymentWatermark, numerator: refundedGoodsMinor, denominator: paidGoodsMinor, rate: paidGoodsMinor ? refundedGoodsMinor / paidGoodsMinor : null, unit: 'CNY_minor', ontology_version: 'v2', mapping_version: 1, as_of: referenceTime, projection_snapshot_id: `${prefix}-REFUND-AMOUNT` });
    const contributingReceipt = systems.warehouse_receipts.find((row)=>row.tenant_id===tenant.tenantId&&orders.some((order)=>order.rmaId===row.rma_id));
    if (contributingReceipt) {
      const rma = systems.return_authorizations.find((row)=>row.tenant_id===tenant.tenantId&&row.rma_id===contributingReceipt.rma_id);
      const order = orders.find((row)=>row.rmaId===rma.rma_id);
      const inspectionEvent = systems.events.find((row)=>row.tenant_id===tenant.tenantId&&row.aggregate_id===rma.rma_id&&row.type==='warehouse.return.inspected');
      const traceId = `SYN-TRACE-${tenant.code}-${String(profile.productNo).padStart(2,'0')}`;
      const snapshotId = `${prefix}-RETURN`;
      systems.traces.push({ tenant_id: tenant.tenantId, trace_id: traceId, spans: ['api.request','analytics.query','wms.projection','investigation.explain'], status: 'completed', synthetic: true });
      systems.anomaly_evidence.push({ tenant_id: tenant.tenantId, evidence_id: `SYN-EVID-${tenant.code}-${String(profile.productNo).padStart(2,'0')}`, source_system: 'WMS', source_record_id: contributingReceipt.source_record_id, trace_id: traceId, event_id: inspectionEvent.event_id, snapshot_id: snapshotId, kind: 'synthetic_quality_contributor', product_id: productId, reason_code: profile.reason, content: `Synthetic ${productId} cohort return facts are derived from ${order.orderId} and its WMS inspection.` });
    }
  }
}
systems.synthetic_batch = { target_orders: 1000, generated_orders: syntheticOrders.length, tenants: tenantProfiles.length, products: syntheticProfiles.length * tenantProfiles.length, cohort: '2026-09', synthetic_only: true };
write('mock/v2/systems.json', systems);

const ontology = `@prefix : <urn:after-sales-agent:ontology:v2#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

:AfterSalesOntology a owl:Ontology ; owl:versionInfo "2.0" .
:Product a owl:Class ; owl:equivalentClass :CanonicalProduct .
:CanonicalProduct a owl:Class .
:SourceProduct a owl:Class .
:SourceProductMapping a owl:Class .
:Evidence a owl:Class .
:MetricDefinition a owl:Class .
:OntologyRelease a owl:Class .
:Order a owl:Class .
:OrderLine a owl:Class .
:ShipmentBatch a owl:Class .
:ReturnAuthorization a owl:Class .
:WarehouseReceipt a owl:Class .
:Inspection a owl:Class .
:Payment a owl:Class .
:RefundExecution a owl:Class .
:MetricSnapshot a owl:Class .
:AnomalyInvestigation a owl:Class .
:Tenant a owl:Class .
:belongsToTenant a owl:ObjectProperty ; rdfs:range :Tenant .
:mapsToCanonicalProduct a owl:ObjectProperty ; rdfs:domain :SourceProductMapping ; rdfs:range :CanonicalProduct .
:referencesOrder a owl:ObjectProperty ; rdfs:range :Order .
:hasEvidence a owl:ObjectProperty ; rdfs:range :Evidence .
:sourceSystem a owl:DatatypeProperty ; rdfs:range xsd:string .
:sourceRecordId a owl:DatatypeProperty ; rdfs:range xsd:string .
:canonicalProductId a owl:DatatypeProperty ; rdfs:range xsd:string .
:mappingVersion a owl:DatatypeProperty ; rdfs:range xsd:positiveInteger .
:mappingStatus a owl:DatatypeProperty ; rdfs:range xsd:string .
:validFrom a owl:DatatypeProperty ; rdfs:range xsd:date .
:validTo a owl:DatatypeProperty ; rdfs:range xsd:date .
:evidenceId a owl:DatatypeProperty ; rdfs:range xsd:string .
:evidenceType a owl:DatatypeProperty ; rdfs:range xsd:string .
:observedAt a owl:DatatypeProperty ; rdfs:range xsd:dateTime .
:metricId a owl:DatatypeProperty ; rdfs:range xsd:string .
:metricVersion a owl:DatatypeProperty ; rdfs:range xsd:positiveInteger .
:numeratorDefinition a owl:DatatypeProperty ; rdfs:range xsd:string .
:denominatorDefinition a owl:DatatypeProperty ; rdfs:range xsd:string .
:sampleUnit a owl:DatatypeProperty ; rdfs:range xsd:string .
:formula a owl:DatatypeProperty ; rdfs:range xsd:string .
:businessApprovalStatus a owl:DatatypeProperty ; rdfs:range xsd:string .
:productionEnabled a owl:DatatypeProperty ; rdfs:range xsd:boolean .
:releaseId a owl:DatatypeProperty ; rdfs:range xsd:string .
:schemaVersion a owl:DatatypeProperty ; rdfs:range xsd:string .
:manifestDigest a owl:DatatypeProperty ; rdfs:range xsd:string .
:releaseStatus a owl:DatatypeProperty ; rdfs:range xsd:string .
:releaseRevision a owl:DatatypeProperty ; rdfs:range xsd:positiveInteger .`;
const shacl = `@prefix : <urn:after-sales-agent:ontology:v2#> .
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

:CanonicalProductShape a sh:NodeShape ; sh:targetClass :CanonicalProduct ;
  sh:property [ sh:path :canonicalProductId ; sh:datatype xsd:string ; sh:minCount 1 ; sh:maxCount 1 ; sh:minLength 1 ] ;
  sh:property [ sh:path :belongsToTenant ; sh:class :Tenant ; sh:minCount 1 ; sh:maxCount 1 ] .
:SourceProductMappingShape a sh:NodeShape ; sh:targetClass :SourceProductMapping ;
  sh:property [ sh:path :sourceSystem ; sh:datatype xsd:string ; sh:minCount 1 ; sh:maxCount 1 ; sh:minLength 1 ] ;
  sh:property [ sh:path :sourceRecordId ; sh:datatype xsd:string ; sh:minCount 1 ; sh:maxCount 1 ; sh:minLength 1 ] ;
  sh:property [ sh:path :belongsToTenant ; sh:class :Tenant ; sh:minCount 1 ; sh:maxCount 1 ] ;
  sh:property [ sh:path :mappingVersion ; sh:datatype xsd:positiveInteger ; sh:minCount 1 ; sh:maxCount 1 ] ;
  sh:property [ sh:path :mappingStatus ; sh:datatype xsd:string ; sh:minCount 1 ; sh:maxCount 1 ; sh:in ( "active" "ambiguous" "unmapped" "expired" ) ] ;
  sh:property [ sh:path :validFrom ; sh:datatype xsd:date ; sh:minCount 1 ; sh:maxCount 1 ] ;
  sh:property [ sh:path :validTo ; sh:datatype xsd:date ; sh:maxCount 1 ] ;
  sh:xone ( :ActiveMappingShape :AmbiguousMappingShape :UnresolvedMappingShape ) ;
  sh:sparql [
    sh:message "An ambiguous source key must have at least two distinct canonical candidates." ;
    sh:select """
      SELECT $this WHERE {
        $this :mappingStatus "ambiguous" ; :belongsToTenant ?tenant ; :sourceSystem ?system ; :sourceRecordId ?sourceId ; :validFrom ?validFrom .
        OPTIONAL { $this :validTo ?validTo . }
        { SELECT ?tenant ?system ?sourceId ?validFrom ?validTo (COUNT(DISTINCT ?target) AS ?candidateCount) WHERE {
            ?candidate a :SourceProductMapping ; :mappingStatus "ambiguous" ; :belongsToTenant ?tenant ; :sourceSystem ?system ; :sourceRecordId ?sourceId ; :validFrom ?validFrom ; :mapsToCanonicalProduct ?target .
            OPTIONAL { ?candidate :validTo ?validTo . }
          } GROUP BY ?tenant ?system ?sourceId ?validFrom ?validTo }
        FILTER (?candidateCount < 2)
      }
    """
  ] .
:ActiveMappingShape a sh:NodeShape ; sh:property [ sh:path :mappingStatus ; sh:hasValue "active" ] ;
  sh:property [ sh:path :mapsToCanonicalProduct ; sh:class :CanonicalProduct ; sh:minCount 1 ; sh:maxCount 1 ] .
:AmbiguousMappingShape a sh:NodeShape ; sh:property [ sh:path :mappingStatus ; sh:hasValue "ambiguous" ] ;
  sh:property [ sh:path :mapsToCanonicalProduct ; sh:class :CanonicalProduct ; sh:minCount 1 ; sh:maxCount 1 ] .
:UnresolvedMappingShape a sh:NodeShape ; sh:property [ sh:path :mappingStatus ; sh:in ( "unmapped" "expired" ) ] ;
  sh:property [ sh:path :mapsToCanonicalProduct ; sh:maxCount 0 ] .
:EvidenceShape a sh:NodeShape ; sh:targetClass :Evidence ;
  sh:property [ sh:path :evidenceId ; sh:datatype xsd:string ; sh:minCount 1 ; sh:maxCount 1 ; sh:minLength 1 ] ;
  sh:property [ sh:path :evidenceType ; sh:datatype xsd:string ; sh:minCount 1 ; sh:maxCount 1 ] ;
  sh:property [ sh:path :sourceRecordId ; sh:datatype xsd:string ; sh:minCount 1 ; sh:maxCount 1 ] ;
  sh:property [ sh:path :sourceSystem ; sh:datatype xsd:string ; sh:minCount 1 ; sh:maxCount 1 ] ;
  sh:property [ sh:path :belongsToTenant ; sh:class :Tenant ; sh:minCount 1 ; sh:maxCount 1 ] ;
  sh:property [ sh:path :observedAt ; sh:datatype xsd:dateTime ; sh:minCount 1 ; sh:maxCount 1 ] .
:MetricDefinitionShape a sh:NodeShape ; sh:targetClass :MetricDefinition ;
  sh:property [ sh:path :metricId ; sh:datatype xsd:string ; sh:minCount 1 ; sh:maxCount 1 ] ;
  sh:property [ sh:path :metricVersion ; sh:datatype xsd:positiveInteger ; sh:minCount 1 ; sh:maxCount 1 ] ;
  sh:property [ sh:path :numeratorDefinition ; sh:datatype xsd:string ; sh:minCount 1 ; sh:maxCount 1 ] ;
  sh:property [ sh:path :denominatorDefinition ; sh:datatype xsd:string ; sh:minCount 1 ; sh:maxCount 1 ] ;
  sh:property [ sh:path :businessApprovalStatus ; sh:datatype xsd:string ; sh:minCount 1 ; sh:maxCount 1 ; sh:in ( "pending" "approved" "rejected" ) ] ;
  sh:xone ( :PendingMetricShape :ApprovedMetricShape :RejectedMetricShape ) .
:PendingMetricShape a sh:NodeShape ; sh:property [ sh:path :businessApprovalStatus ; sh:hasValue "pending" ] ;
  sh:property [ sh:path :productionEnabled ; sh:hasValue false ] .
:ApprovedMetricShape a sh:NodeShape ; sh:property [ sh:path :businessApprovalStatus ; sh:hasValue "approved" ] ;
  sh:property [ sh:path :productionEnabled ; sh:hasValue true ] ;
  sh:property [ sh:path :formula ; sh:datatype xsd:string ; sh:minCount 1 ; sh:maxCount 1 ] ;
  sh:property [ sh:path :sampleUnit ; sh:datatype xsd:string ; sh:minCount 1 ; sh:maxCount 1 ] .
:RejectedMetricShape a sh:NodeShape ; sh:property [ sh:path :businessApprovalStatus ; sh:hasValue "rejected" ] ;
  sh:property [ sh:path :productionEnabled ; sh:hasValue false ] .
:OntologyReleaseShape a sh:NodeShape ; sh:targetClass :OntologyRelease ;
  sh:property [ sh:path :releaseId ; sh:datatype xsd:string ; sh:minCount 1 ; sh:maxCount 1 ] ;
  sh:property [ sh:path :schemaVersion ; sh:datatype xsd:string ; sh:minCount 1 ; sh:maxCount 1 ] ;
  sh:property [ sh:path :manifestDigest ; sh:datatype xsd:string ; sh:minCount 1 ; sh:maxCount 1 ; sh:pattern "^[a-f0-9]{64}$" ] ;
  sh:property [ sh:path :releaseRevision ; sh:datatype xsd:positiveInteger ; sh:minCount 1 ; sh:maxCount 1 ] ;
  sh:property [ sh:path :releaseStatus ; sh:datatype xsd:string ; sh:minCount 1 ; sh:maxCount 1 ; sh:in ( "candidate" "validated" "active" "retired" ) ] .`;
write('knowledge/v2/ontology/core.ttl', ontology);
write('knowledge/v2/ontology/constraints.shacl.ttl', shacl);
write('knowledge/v2/ontology/source_mappings.json', systems.product_mappings);
write('knowledge/v2/ontology/metrics.json', [
  { metric_id: 'return_passed_item_rate', display_name: '验收通过退货件数率', version: 1, numerator: 'WMS accepted return item quantity', denominator: 'signed item quantity in the same shipment cohort', formula: 'sum(numerator) / sum(denominator)', sample_unit: 'signed_item_quantity', minimum_sample: 100, window_days: 30, ranking_role: 'supervisor_default_candidate', default_after_approval: true, production_enabled: false, mock_evaluable: true, definition_status: 'pending_business_finance_approval', business_approval: 'pending' },
  { metric_id: 'return_application_item_rate', display_name: '退货申请率', version: 1, numerator_options: ['submitted_return_application_count', 'return_application_line_count', 'requested_return_item_quantity'], denominator_options: ['eligible_paid_order_count', 'eligible_paid_order_line_count', 'eligible_paid_item_quantity'], cohort_time_options: ['application_created_at', 'paid_at', 'shipment_cohort'], formula: null, sample_unit: null, minimum_sample: 100, window_days: 30, ranking_role: 'independent_metric', default_after_approval: false, production_enabled: false, mock_evaluable: false, definition_status: 'pending_business_finance_approval', calculation_status: 'blocked_pending_business_finance_approval', business_approval: 'pending' },
  { metric_id: 'refund_successful_line_rate', version: 1, numerator: 'successfully refunded payment detail count', denominator: 'paid detail count in the same payment cohort', formula: 'sum(numerator) / sum(denominator)', sample_unit: 'paid_detail_count', minimum_sample: 100, window_days: 30, business_approval: 'pending', production_enabled: false },
  { metric_id: 'refund_goods_amount_rate', version: 1, numerator: 'successfully refunded goods amount in minor currency units', denominator: 'paid goods amount in minor currency units', formula: 'sum(numerator) / sum(denominator)', shipping_treatment: 'reported separately', sample_unit: 'paid_detail_count', minimum_sample: 100, window_days: 30, business_approval: 'pending', production_enabled: false }
]);
write('contracts/metric-approval-register.json', {
  schema_version: '1.0',
  register_version: 1,
  status: 'pending_external_approval',
  rule: 'Mock evaluation may use explicitly marked synthetic data. Production reporting and ranking remain disabled until both business and finance approvals are recorded for each metric.',
  metrics: [
    { metric_id: 'return_passed_item_rate', design_decision: 'selected_supervisor_default_candidate', definition: 'WMS accepted return item quantity divided by signed item quantity in the same shipment cohort and fixed observation window; report under the qualified name 验收通过退货件数率.', business_approval: { status: 'pending', approver: null, approved_at: null, evidence_ref: null }, finance_approval: { status: 'pending', approver: null, approved_at: null, evidence_ref: null }, production_enabled: false },
    { metric_id: 'return_application_item_rate', design_decision: 'separate_independent_metric', definition: 'Pending selection of numerator unit, eligible denominator unit, and cohort attribution time.', business_approval: { status: 'pending', approver: null, approved_at: null, evidence_ref: null }, finance_approval: { status: 'pending', approver: null, approved_at: null, evidence_ref: null }, production_enabled: false },
    { metric_id: 'refund_successful_line_rate', design_decision: 'separate_refund_metric', definition: 'Successfully refunded payment detail count divided by paid detail count in the same payment cohort.', business_approval: { status: 'pending', approver: null, approved_at: null, evidence_ref: null }, finance_approval: { status: 'pending', approver: null, approved_at: null, evidence_ref: null }, production_enabled: false },
    { metric_id: 'refund_goods_amount_rate', design_decision: 'separate_refund_metric', definition: 'Successfully refunded goods amount divided by paid goods amount in minor currency units; shipping is reported separately.', business_approval: { status: 'pending', approver: null, approved_at: null, evidence_ref: null }, finance_approval: { status: 'pending', approver: null, approved_at: null, evidence_ref: null }, production_enabled: false }
  ]
});
write('knowledge/v2/evidence.json', systems.anomaly_evidence);
const knowledgeCorpus = {
  schema_version: '2.0', dataset_version: version, synthetic_only: true, release_id: 'KNOWLEDGE-RELEASE-2026-10',
  coverage_tags: ['synonym','typo','multi_hop','expired_version','draft_conflict','long_tail','no_answer','tenant_scope'],
  documents: [
    { tenant_id:'tenant-north', document_id:'DOC-RETURN-CURRENT', policy_id:'POL-RETURN-01', version:2, status:'published', valid_from:'2026-01-01', valid_to:null, title:'Return policy 2026', aliases:['return','send back','return request','退货','退回','申请退货','退货政策'], content:'Submit a return request within 7 days after delivery. Refund approval, warehouse receipt and payment execution are separate states.', tags:['return','synonym','current'], evidence_id:'KNOW-EV-RETURN-02' },
    { tenant_id:'tenant-north', document_id:'DOC-RETURN-LEGACY', policy_id:'POL-RETURN-01', version:1, status:'retired', valid_from:'2025-01-01', valid_to:'2025-12-31', title:'Return policy archived', aliases:['old return policy'], content:'Retired policy; use only for historical transactions under its effective date.', tags:['return','expired_version'], evidence_id:'KNOW-EV-RETURN-01' },
    { tenant_id:'tenant-north', document_id:'DOC-RETURN-DRAFT', policy_id:'POL-RETURN-01', version:3, status:'draft', valid_from:'2026-11-01', valid_to:null, title:'Return policy draft', aliases:[], content:'Unapproved draft proposing a 30-day window. Do not use in customer answers.', tags:['return','draft_conflict'], evidence_id:'KNOW-EV-RETURN-DRAFT' },
    { tenant_id:'tenant-north', document_id:'DOC-REFUND-CURRENT', policy_id:'POL-REFUND-02', version:1, status:'published', valid_from:'2026-01-01', valid_to:null, title:'Refund review and settlement', aliases:['refund status','refund arrival','money back'], content:'Show review, warehouse and funds status separately. Only an authoritative payment query confirms settlement.', tags:['refund','three_state'], evidence_id:'KNOW-EV-REFUND-01' },
    { tenant_id:'tenant-north', document_id:'DOC-PRODUCT-AIR-01', policy_id:null, version:1, status:'published', valid_from:'2026-01-01', valid_to:null, title:'Air purifier A product guide', aliases:['air purifier A','purifier A','air purifer A','净化器A','空气净化器A'], content:'Product product-air-01 uses filter-f-01. Similar names do not imply compatibility. Misspelled alias is for retrieval testing.', tags:['product','synonym','typo'], evidence_id:'KNOW-EV-PRODUCT-01' },
    { tenant_id:'tenant-north', document_id:'DOC-FILTER-F-01', policy_id:null, version:1, status:'published', valid_from:'2026-01-01', valid_to:null, title:'Filter F-01 compatibility', aliases:['F01 filter'], content:'Filter filter-f-01 is compatible only with product-air-01.', tags:['accessory','multi_hop'], evidence_id:'KNOW-EV-FILTER-01' },
    { tenant_id:'tenant-north', document_id:'DOC-FAQ-FILTER-01', policy_id:null, version:1, status:'published', valid_from:'2026-01-01', valid_to:null, title:'FAQ: Filter F-01 compatibility', aliases:['Can purifier A use F-01?','净化器A滤芯'], content:'FAQ: Can Air purifier A use Filter F-01? Yes. Filter F-01 is compatible with product-air-01. Check the product model before replacement.', tags:['faq','accessory'], evidence_id:'KNOW-EV-FAQ-FILTER-01' },
    { tenant_id:'tenant-north', document_id:'DOC-FAULT-F23', policy_id:null, version:1, status:'published', valid_from:'2026-02-01', valid_to:null, title:'Fault code F-23 troubleshooting', aliases:['F23','error code F-23'], content:'F-23 indicates an intake sensor issue. Record test results in the work order. This guide does not establish refund eligibility.', tags:['fault_code','long_tail'], evidence_id:'KNOW-EV-FAULT-01' },
    { tenant_id:'tenant-north', document_id:'DOC-SERIAL-RETURN', policy_id:'POL-RETURN-01', version:1, status:'published', valid_from:'2026-04-01', valid_to:null, title:'Serial-number return inspection', aliases:['serial number return','SN return'], content:'Record serial number and inspection result. Quarantine mismatches; do not infer a refund amount.', tags:['long_tail','warehouse'], evidence_id:'KNOW-EV-SERIAL-01' },
    { tenant_id:'tenant-south', document_id:'DOC-SOUTH-RETURN', policy_id:'POL-RETURN-91', version:1, status:'published', valid_from:'2026-01-01', valid_to:null, title:'South tenant return policy', aliases:['return policy'], content:'This policy applies only to tenant-south. Never use a same-name policy from another tenant.', tags:['tenant_scope','return'], evidence_id:'KNOW-EV-SOUTH-01' }
  ], evidence: [], graph: {
    nodes: [
      { tenant_id:'tenant-north', node_id:'product-air-01', kind:'Product', label:'Air purifier A', product_id:'product-air-01', name:'Air purifier A', model:'A', status:'active', version:1 },
      { tenant_id:'tenant-north', node_id:'filter-f-01', kind:'Accessory', label:'Filter F-01', accessory_id:'filter-f-01', name:'Filter F-01', model:'F-01', status:'active', version:1 },
      { tenant_id:'tenant-north', node_id:'fault-f23', kind:'FaultSymptom', label:'F-23', fault_id:'fault-f23', name:'F-23', description:'Intake sensor issue', severity:'unknown', version:1 },
      { tenant_id:'tenant-north', node_id:'service-sensor-check', kind:'ServiceItem', label:'Sensor inspection', service_id:'service-sensor-check', name:'Sensor inspection', description:'Record intake sensor test results in the work order', version:1 },
      { tenant_id:'tenant-north', node_id:'category-air-01', kind:'ProductCategory', label:'Air purifier', category_id:'category-air-01', name:'Air purifier', version:1 },
      { tenant_id:'tenant-north', node_id:'policy-return-01', kind:'Policy', label:'Return policy 2026', policy_id:'POL-RETURN-01', name:'Return policy 2026', policy_type:'return', status:'published', version:2, effective_from:'2026-01-01', effective_to:'9999-12-31' },
      { tenant_id:'tenant-north', node_id:'policy-refund-02', kind:'Policy', label:'Refund policy', policy_id:'POL-REFUND-02', name:'Refund policy', policy_type:'refund', status:'published', version:1, effective_from:'2026-01-01', effective_to:'9999-12-31' },
      { tenant_id:'tenant-south', node_id:'product-air-91', kind:'Product', label:'South purifier A', product_id:'product-air-91', name:'South purifier A', model:'A', status:'active', version:1 },
      { tenant_id:'tenant-south', node_id:'policy-return-91', kind:'Policy', label:'South return policy', policy_id:'POL-RETURN-91', name:'South return policy', policy_type:'return', status:'published', version:1, effective_from:'2026-01-01', effective_to:'9999-12-31' }
    ], edges: [
      { tenant_id:'tenant-north', edge_id:'KG-001', from:'product-air-01', relation:'COMPATIBLE_WITH', to:'filter-f-01', compatibility:'compatible', document_id:'DOC-FILTER-F-01', evidence_id:'KNOW-EV-FILTER-01' },
      { tenant_id:'tenant-north', edge_id:'KG-002', from:'product-air-01', relation:'HAS_POLICY', to:'policy-return-01', priority:1, document_id:'DOC-RETURN-CURRENT', evidence_id:'KNOW-EV-RETURN-02' },
      { tenant_id:'tenant-north', edge_id:'KG-003', from:'product-air-01', relation:'HAS_FAULT_SYMPTOM', to:'fault-f23', severity:'unknown', document_id:'DOC-FAULT-F23', evidence_id:'KNOW-EV-FAULT-01' },
      { tenant_id:'tenant-north', edge_id:'KG-004', from:'fault-f23', relation:'SUGGESTS_SERVICE', to:'service-sensor-check', priority:1, document_id:'DOC-FAULT-F23', evidence_id:'KNOW-EV-FAULT-01' },
      { tenant_id:'tenant-north', edge_id:'KG-005', from:'policy-return-01', relation:'APPLIES_TO', to:'category-air-01', conditions:'Air purifier category', document_id:'DOC-RETURN-CURRENT', evidence_id:'KNOW-EV-RETURN-02' },
      { tenant_id:'tenant-north', edge_id:'KG-006', from:'product-air-01', relation:'HAS_POLICY', to:'policy-refund-02', priority:1, document_id:'DOC-REFUND-CURRENT', evidence_id:'KNOW-EV-REFUND-01' },
      { tenant_id:'tenant-south', edge_id:'KG-091', from:'product-air-91', relation:'HAS_POLICY', to:'policy-return-91', priority:1, document_id:'DOC-SOUTH-RETURN', evidence_id:'KNOW-EV-SOUTH-01' }
    ]
  }
};
knowledgeCorpus.evidence = knowledgeCorpus.documents.map((doc)=>({tenant_id:doc.tenant_id,evidence_id:doc.evidence_id,source_system:'KNOWLEDGE',source_record_id:doc.document_id,source_version:`${doc.document_id}:v${doc.version}`,document_id:doc.document_id,valid_from:doc.valid_from,valid_to:doc.valid_to,status:doc.status}));
const knowledgeEvidence = new Map(knowledgeCorpus.evidence.map((item)=>[`${item.tenant_id}:${item.evidence_id}`,item]));
for (const edge of knowledgeCorpus.graph.edges) {
  const source = knowledgeEvidence.get(`${edge.tenant_id}:${edge.evidence_id}`);
  if (!source || source.document_id !== edge.document_id) throw new Error(`知识关系证据不匹配: ${edge.edge_id}`);
  edge.version = source.source_version;
  edge.valid_from = source.valid_from;
  edge.valid_to = source.valid_to || '9999-12-31';
}
write('knowledge/v2/corpus.json', knowledgeCorpus);

const cases = [
  [2,'FR-02','intent','refund','refund_eligibility','classify_intent','must not create a refund',null],
  [3,'FR-03','supervisor','查询退货率和退款率最高的商品','analytics.query','read_only','return and refund rate with numerator and denominator',null],
  [31,'FR-14','react','query an order then explain the result','order.query','bounded_tool_loop','no unauthorized write',null],
  [32,'FR-15','plan_execute','investigate a cross-system return issue','investigation.create','persisted_plan','plan has evidence refs',null],
  [33,'FR-16','reflection','verify a conflicting source result','source.reconcile','selective_reflection','unknown until verified',null],
  [95,'FR-20','ontology','PIM、OMS、WMS商品编码不一致','ontology.resolve_product','mapping_versioned','canonical product product-air-01',null],
  [96,'FR-20','ontology','相同订单号跨租户且映射有歧义','ontology.validate_scope','deny_ambiguous','deny cross tenant and ambiguous mapping',null],
  [97,'FR-21','warehouse','分批收货、部分验收、超收与重复登记','warehouse.inspect_return','idempotent_receipt','passed quantity 10; reject excess',null],
  [98,'FR-21','warehouse','需要退货验收与获批仅退款','refund.check_prerequisites','gate_payment_by_return','required return blocks payment; authorized not_required permits',null],
  [99,'FR-22','payment','已批准、已确认且完成验收的退款执行','payment.execute_refund','controlled_execute','successful source record only',null],
  [100,'FR-22','payment','退款提交超时并重启后恢复查询','payment.query_operation','same_operation_recovery','one refund execution',null],
  [101,'FR-22','payment','伪造、重放、乱序或金额不符的回调','payment.verify_callback','reject_invalid_callback','no ledger posting for invalid callback',null],
  [102,'FR-22','payment','部分退款并发、重复支付与多明细分摊','payment.allocate_refund','conservation_and_idempotency','refund allocation conserves amounts',null],
  [103,'FR-22','payment','旧审批版本、无权限执行或金额增加','payment.execute_refund','revalidate_auth_and_revision','deny stale or unauthorized execution',null],
  [104,'FR-23','analytics','跨月退款、分批签收和观察窗口未成熟','analytics.query_return_rate','fixed_cohort_window','same cohort and fixed window', 'EVID-RETURN-001|EVID-REFUND-001'],
  [105,'FR-23','analytics','SKU汇总、重复流水、零分母及小样本','analytics.aggregate_metrics','weighted_aggregate_quality_gate','aggregate numerator and denominator; null for zero', 'EVID-QUALITY-001'],
  [106,'FR-23','analytics','源缺失、映射缺失、水位滞后和对账差异','analytics.check_data_quality','block_unreliable_ranking','quality warning; no fabricated zero', 'EVID-QUALITY-001'],
  [107,'FR-23','analytics','跨团队主管查询及越权下钻','analytics.query_authorized_scope','scope_filter_before_aggregate','no unauthorized detail',null],
  [108,'FR-20|FR-23','ontology','版本升级、迟到更正和投影重建','analytics.replay_snapshot','versioned_replay','same snapshot reproduces prior result', 'EVID-RETURN-001'],
  [109,'FR-21|FR-22','warehouse|payment','模拟WMS/支付闭环及生产环境隔离','integration.verify_environment','simulation_only','environment is mock; no production credential',null],
  [110,'FR-24','investigation','产品退货率突增并定位可复算因素','investigation.create','evidence_backed_explanation','observed 0.1; baseline 0.04; contribution is traceable', 'EVID-RETURN-BASE-001|EVID-RETURN-001'],
  [111,'FR-24','investigation','按SKU、渠道、区域或批次分解贡献','investigation.decompose_contribution','recompute_group_contribution','contribution sums to observed delta', 'EVID-RETURN-001'],
  [112,'FR-24','investigation','未确认的自由文本原因不得作为根因','investigation.classify_reason','require_confirmed_reason_code','unconfirmed reason excluded', 'EVID-RETURN-001'],
  [113,'FR-24','investigation','水位滞后、歧义映射或金额对账差异','investigation.check_quality_gates','stop_root_cause_ranking','identify first failed quality gate', 'EVID-QUALITY-001'],
  [114,'FR-24','investigation','样本不足、基准为空或窗口未成熟','investigation.compare_windows','mark_not_comparable','no confirmed anomaly claim', 'EVID-QUALITY-001'],
  [115,'FR-24','investigation','调查下钻授权与固定快照重放','investigation.read_evidence','authorize_each_source_record','same snapshot and evidence refs', 'EVID-RETURN-001'],
  [116,'FR-24','investigation','源事实修正后追加调查修订','investigation.revise','append_revision','prior revision preserved', 'EVID-RETURN-001'],
  [117,'FR-24','investigation','外部依赖超时与业务拒绝的首因定位','investigation.trace_dependency','trace_first_failed_span','identify dependency span', 'EVID-PAYMENT-001'],
  [118,'FR-24','investigation','事件重复、积压和死信链路追查','investigation.trace_event','follow_event_causation','event producer and consumer are traceable', 'EVID-RETURN-001'],
  [119,'FR-24','investigation','OMS、WMS、支付金额或状态不一致','investigation.compare_authoritative_sources','list_source_conflict','show each source owner and version', 'EVID-PAYMENT-001'],
  [120,'FR-24','investigation','知识索引部分失败和发布版本追查','investigation.trace_knowledge_release','trace_release_manifest','report partial indexing; no guessed model cause',null],
  [121,'FR-24','investigation','遥测缺失或采样丢失','investigation.report_coverage','mark_evidence_gap','state unobservable range',null],
  [122,'FR-24','investigation','跨租户证据访问与聚合侧信道','investigation.read_evidence','tenant_and_subject_scope','deny cross tenant before aggregation',null],
  [123,'FR-24','investigation','重复调查、快照固定与结论修订','investigation.revise','immutable_revision_history','keep prior snapshots and conclusions', 'EVID-RETURN-001'],
  [6,'FR-07','refund','符合退款条件后先展示报价并等待客户确认','refund.quote','quote_before_confirmation','show amount and require explicit confirmation; create no request',null],
  [7,'FR-07','refund','客户完成绑定确认和一次性验证后创建退款申请','refund.create_request','confirmed_request_creation','create pending_review only after all server checks',null],
  [69,'FR-07','refund','未履约订单按商品款与运费计算退款报价','refund.quote','full_unfulfilled_quote','quote 11000 minor units and create no request before confirmation',null],
  [70,'FR-07','refund','已签收部分退款订单重新计算可申请金额','refund.quote','signed_partial_refund_quote','quote remaining goods amount 8000 minor units; exclude shipping',null],
  [71,'FR-07','refund','未支付、全额已退、缺原因或金额矛盾','refund.evaluate_request','reject_or_ask_without_creation','do not create request; ask or require manual verification',null],
  [72,'FR-07','refund','报价后订单或政策版本变化','refund.confirm_quote','reject_stale_quote','reject old confirmation and require a fresh quote',null],
  [73,'FR-07','refund','退款申请重复提交和幂等冲突','refund.create_request','idempotent_request_creation','same request returns original; changed payload conflicts; no extra reservation',null],
  [74,'FR-07','refund','获批仅退款请求经过受控支付并以权威成功记账','refund.review_and_execute','approved_then_authoritative_success','review approval is separate; funds succeed only after channel evidence','EVID-PAYMENT-001'],
  [75,'FR-07','refund','退货要求或审核消费者故障时禁止自动批准','refund.review','hold_for_manual_review','keep review pending and reservation recoverable',null],
  [76,'FR-07','refund','拒绝审核、重复消息和越权查询','refund.reject_and_query','single_release_and_safe_deny','release reservation once and conceal inaccessible request existence',null],
  [77,'FR-07','refund','申请创建超时后按原幂等键恢复','refund.query_by_idempotency','recover_original_request','return actual request or unknown; never create a duplicate',null],
  [88,'FR-19','workbench','刷新工作台后恢复代客会话与异步任务','workbench.restore_session_task','restore_without_reexecution','restore authorized state without repeating writes',null],
  [89,'FR-19','workbench','工单筛选、确认、分派和状态更新','workbench.update_ticket','scope_revision_idempotency','enforce scope, confirmation, idempotency and revision',null],
  [90,'FR-19','workbench','两名客服并发接收同一转接','workbench.accept_handoff','single_concurrent_accept','one employee succeeds; other receives conflict; no duplicate ticket',null],
  [91,'FR-19','workbench','客服尝试代客户确认退款或改址','workbench.confirm_for_customer','reject_cross_subject_confirmation','only beneficiary customer confirmation can authorize operation',null],
  [92,'FR-19','workbench','主管审核与客服/管理员越权尝试','workbench.review_refund','review_scope_enforced','authorized supervisor may review; other identities denied; approval is not payment',null],
  [93,'FR-19','workbench','客户访问员工接口或其他团队记录','workbench.authorize_employee_resource','deny_out_of_scope_access','deny at server regardless of client route or parameters',null],
  [94,'FR-19','workbench','工作台依赖故障、空结果与未知操作状态','workbench.render_recovery_state','no_fabricated_success','report empty/error/unknown distinctly and refresh by existing operation id',null]
  ,[14,'FR-01','gateway','空问题不得进入业务工具','gateway.validate_input','ask_for_input','no tool call for empty prompt',null]
  ,[41,'FR-04','retrieval','精确型号或错误码关键词命中','knowledge.search','bm25_exact_match','exact lexical result remains eligible without vector hit',null]
  ,[9,'FR-05','security','查询不属于当前主体的订单','order.query','safe_deny','same public error as absent order',null]
  ,[4,'FR-06','logistics','物流轨迹停滞但来源查询成功','logistics.query_tracking','report_source_fact','do not invent delivery time',null]
  ,[5,'FR-07','refund','退款原因缺失','refund.check_prerequisites','ask_reason','must not create refund request',null]
  ,[8,'FR-08','ticket','用户要求投诉客服','ticket.create_or_handoff','confirmed_ticket_only','report ticket only after confirmed creation',null]
  ,[78,'FR-09','contract','请求包含未知MCP字段','mcp.validate_request','reject_unknown_field','reject before domain call',null]
  ,[32,'FR-10|FR-15','workflow','订单物流退款组合只生成必要串行计划','workflow.plan','bounded_plan','no parallel business writes',null]
  ,[21,'FR-11','memory','新会话恢复同一租户的短期会话','memory.read_session','tenant_scoped_restore','restore only same tenant thread',null]
  ,[11,'FR-12','resilience','只读依赖连续瞬态失败','dependency.call','bounded_retry_then_handoff','at most two total attempts',null]
  ,[18,'FR-13','messaging','消费者多次失败后进入死信','event.consume','retry_then_dlq','four total attempts and preserve event identity',null]
  ,[38,'FR-17','checkpoint','旧state版本没有迁移器','checkpoint.restore','fail_closed','do not execute writes from unknown state version',null]
  ,[57,'FR-18','address','用户完成绑定确认后修改本人待发货订单地址','order.update_shipping_address','confirmed_versioned_update','only mock adapter side effect; address body never enters model context',null]
  ,[87,'FR-19','workbench','客服切换到已授权客户','workbench.select_customer','scope_checked','authorized customer only and separate session',null]
  ,[124,'FR-04','retrieval','用同义词和拼写错误检索商品说明','knowledge.search','alias_and_typo_recall','return the canonical product guide with evidence','KNOW-EV-PRODUCT-01']
  ,[125,'FR-04','retrieval','查询产品到配件的多跳兼容关系','knowledge.graph_query','evidence_backed_multi_hop','return product, accessory and supporting document','KNOW-EV-FILTER-01']
  ,[126,'FR-04','retrieval','新旧政策和未发布草稿冲突时只采用有效发布版本','knowledge.policy_query','effective_published_version','exclude retired and draft documents','KNOW-EV-RETURN-02']
  ,[127,'FR-04','retrieval','同名政策不得跨租户引用','knowledge.search','tenant_scoped_retrieval','return only evidence for caller tenant','KNOW-EV-RETURN-02']
  ,[128,'FR-04','retrieval','知识库没有答案时明确说明无证据','knowledge.search','no_answer_abstention','do not invent an answer',null]
];

const toolArgs = (tc) => ({ tc_id: `TC-${String(tc).padStart(2,'0')}` });
const actorForTc = (tc) => {
  if ([6,7,69,70,71,72,73,74,75,76,77].includes(tc)) return { subject_id: 'customer-01', roles: ['customer'], scopes: ['order:read','refund:read','refund:write','customer:verify'] };
  if ([87,88,89,91,93,94].includes(tc)) return { subject_id: 'support-01', roles: ['support'], scopes: ['customer:read','ticket:read','ticket:write','handoff:accept'] };
  if (tc === 90) return { subject_id: 'human-agent-01', roles: ['human_agent'], scopes: ['handoff:read','handoff:accept','ticket:write'] };
  if (tc === 92) return { subject_id: 'supervisor-01', roles: ['supervisor'], scopes: ['refund:review','analytics:read','investigation:read'] };
  return { subject_id: 'supervisor-01', roles: ['supervisor'], scopes: ['analytics:read','investigation:read'] };
};
const evidenceMap = new Map([...systems.anomaly_evidence,...knowledgeCorpus.evidence].map((row)=>[row.evidence_id,row]));
const factsByTc = {
  2:[['intent.name','equals','refund_eligibility'],['intent.requires_order_lookup','equals',true]],
  3:[['metric.return.snapshot_id','equals','METRIC-RETURN-2026-09'],['metric.return.numerator','equals',10],['metric.return.denominator','equals',100],['metric.return.rate','equals',0.1],['metric.refund_line.rate','equals',0.05],['metric.refund_goods_amount.rate','equals',0.05]],
  31:[['agent.tool_budget_respected','equals',true],['business.write_count','equals',0]],
  32:[['plan.evidence_refs_present','equals',true],['plan.resumable','equals',true]],
  33:[['reflection.trigger_reason','equals','source_conflict'],['result.verified','equals',false]],
  95:[['product.canonical_product_id','equals','product-air-01'],['product.mapping_version','equals',2]],
  96:[['mapping.status','equals','ambiguous'],['authorization.allowed','equals',false]],
  97:[['warehouse.passed_quantity','equals',10],['warehouse.rejected_quantity','equals',4],['warehouse.overage_accepted','equals',false]],
  98:[['payment.prerequisite_satisfied','equals',false],['payment.allowed','equals',false]],
  99:[['payment.refund.status','equals','succeeded'],['payment.refund.amount_minor','equals',10000]],
  100:[['payment.refund.operation_count','equals',1],['payment.refund.status','equals','unknown_until_query']],
  101:[['payment.callback.signature_valid','equals',false],['payment.ledger.posting_count','equals',0]],
  102:[['payment.allocations.sum_matches_execution','equals',true],['payment.duplicate_posting_count','equals',0]],
  103:[['payment.execution.allowed','equals',false],['payment.funds_status','equals','not_executed']],
  104:[['metric.return_snapshot_id','equals','METRIC-RETURN-2026-09'],['metric.refund_snapshot_id','equals','METRIC-REFUND-LINE-2026-09'],['metric.return_cohort','equals','signed-2026-09'],['metric.refund_cohort','equals','paid-2026-09'],['metric.quality','equals','complete']],
  105:[['metric.aggregation.method','equals','sum_numerator_over_sum_denominator'],['metric.zero_denominator_value','equals',null],['metric.small_sample_ranked','equals',false]],
  106:[['metric.quality','equals','incomplete'],['metric.ranking_blocked','equals',true]],
  107:[['authorization.filter_applied_before_aggregate','equals',true],['authorization.out_of_scope_details_returned','equals',false]],
  108:[['snapshot.prior_version_preserved','equals',true],['snapshot.replay_matches_original','equals',true]],
  109:[['environment.name','equals','mock'],['production.credentials_used','equals',false]],
  110:[['metric.current.rate','equals',0.1],['metric.baseline.rate','equals',0.04],['metric.delta_percentage_points','equals',0.06]],
  111:[['investigation.contribution_recomputable','equals',true],['investigation.contribution_sum_matches_delta','equals',true]],
  112:[['investigation.unconfirmed_reason_included','equals',false],['investigation.confirmed_reason_code_required','equals',true]],
  113:[['investigation.quality_gate','equals','blocked'],['investigation.root_cause_ranking_available','equals',false]],
  114:[['comparison.comparable','equals',false],['investigation.confirmed_anomaly_claim','equals',false]],
  115:[['authorization.checked_for_each_source_record','equals',true],['snapshot.replay_matches_original','equals',true]],
  116:[['investigation.prior_revision_preserved','equals',true],['investigation.new_revision_appended','equals',true]],
  117:[['trace.first_failed_component','equals','payment.execute'],['trace.failure_type','equals','timeout']],
  118:[['event.event_id','equals','WMS-EVT-0900'],['event.producer_consumer_chain_present','equals',true]],
  119:[['source_conflict.authoritative_owner_listed','equals',true],['source_conflict.source_versions_listed','equals',true]],
  120:[['knowledge_release.status','equals','partial_failure'],['investigation.guessed_model_cause','equals',false]],
  121:[['telemetry.coverage','equals','incomplete'],['investigation.unobservable_range_marked','equals',true]],
  122:[['authorization.cross_tenant_detail_returned','equals',false],['authorization.filter_applied_before_aggregate','equals',true]],
  123:[['investigation.prior_revision_preserved','equals',true],['investigation.snapshot_history_immutable','equals',true]],
  14:[['input.valid','equals',false],['tool_call.count','equals',0]],
  41:[['retrieval.source','equals','bm25'],['retrieval.candidate_eligible','equals',true]],
  9:[['authorization.allowed','equals',false],['resource.existence_disclosed','equals',false]],
  4:[['logistics.source_status','equals','stalled'],['answer.delivery_time_invented','equals',false]],
  5:[['refund.reason_present','equals',false],['refund.request_created','equals',false]],
  8:[['ticket.creation_reported','equals','only_if_confirmed'],['handoff.status_reported','equals','only_if_confirmed']],
  78:[['request.validation','equals','rejected'],['domain.call_count','equals',0]],
  32:[['plan.write_operations_parallel','equals',false],['plan.authorization_source','equals','server_context']],
  21:[['session.tenant_match','equals',true],['session.cross_tenant_data_returned','equals',false]],
  11:[['dependency.total_attempts','equals',2],['request.final_action','equals','handoff']],
  18:[['event.total_attempts','equals',4],['event.dead_lettered','equals',true]],
  38:[['checkpoint.restore_status','equals','failed_closed'],['business.write_count','equals',0]],
  57:[['address.confirmation_bound','equals',true],['address.expected_version_checked','equals',true],['address.model_context_contains_full_address','equals',false]],
  87:[['workbench.scope_checked','equals',true],['workbench.out_of_scope_customer_returned','equals',false]]
  ,124:[['retrieval.alias_or_typo_matched','equals',true],['retrieval.evidence_id','equals','KNOW-EV-PRODUCT-01']]
  ,125:[['graph.path_length','equals',2],['graph.evidence_backed','equals',true]]
  ,126:[['retrieval.selected_document_id','equals','DOC-RETURN-CURRENT'],['retrieval.draft_or_retired_selected','equals',false]]
  ,127:[['retrieval.all_results_match_tenant','equals',true],['retrieval.cross_tenant_results','equals',0]]
  ,128:[['retrieval.answer_status','equals','no_evidence'],['retrieval.unsupported_claim_count','equals',0]]
  ,6:[['refund.quote.displayed_before_confirmation','equals',true],['refund.request_created','equals',false]]
  ,7:[['refund.request.status','equals','pending_review'],['refund.confirmation.bound_to_request','equals',true],['refund.one_time_verification.consumed','equals',true]]
  ,69:[['refund.quote.amount_minor','equals',11000],['refund.request_created','equals',false]]
  ,70:[['refund.quote.goods_amount_minor','equals',8000],['refund.quote.shipping_amount_minor','equals',0],['refund.request.status','equals','pending_review']]
  ,71:[['refund.request_created','equals',false],['refund.result','equals','ineligible_or_needs_clarification']]
  ,72:[['refund.confirmation.accepted','equals',false],['refund.quote.refresh_required','equals',true]]
  ,73:[['refund.duplicate_request_count','equals',0],['refund.reservation.count','equals',1],['refund.changed_payload_conflict','equals',true]]
  ,74:[['refund.review_status','equals','approved'],['payment.funds_status','equals','succeeded'],['payment.ledger.requires_authoritative_evidence','equals',true]]
  ,75:[['refund.review_status','equals','reviewing'],['refund.auto_approved','equals',false],['refund.reservation.recoverable','equals',true]]
  ,76:[['refund.reservation.release_count','equals',1],['refund.inaccessible_resource_disclosed','equals',false]]
  ,77:[['refund.request.creation_count','equals',1],['refund.recovery.reuses_idempotency_key','equals',true]]
  ,88:[['workbench.restored_session_matches_original','equals',true],['workbench.reexecuted_write_count','equals',0]]
  ,89:[['workbench.scope_and_revision_enforced','equals',true],['workbench.internal_message_leaked','equals',false]]
  ,90:[['workbench.concurrent_accept.success_count','equals',1],['workbench.concurrent_accept.conflict_count','equals',1]]
  ,91:[['confirmation.actor_matches_beneficiary','equals',false],['business.write_count','equals',0]]
  ,92:[['workbench.review_scope_enforced','equals',true],['refund.approved_is_funds_success','equals',false]]
  ,93:[['authorization.out_of_scope_access_allowed','equals',false],['authorization.server_side_enforced','equals',true]]
  ,94:[['workbench.operation_status','equals','unknown'],['workbench.fabricated_success','equals',false],['workbench.refresh_reuses_operation_id','equals',true]]
};
const caseIdCounts = new Map();
const scenarios = cases.map(([tc, fr, domain, prompt, tool, expectedStatus, expectedNote, evidence]) => {
  const count = caseIdCounts.get(tc) || 0;
  caseIdCounts.set(tc, count + 1);
  const base = `v2-tc-${String(tc).padStart(2,'0')}`;
  const id = count === 0 ? base : `${base}-${domain.replace(/[^a-z0-9]+/gi,'-')}-${count + 1}`;
  const effects = domain === 'payment' || domain === 'warehouse'
    ? [{ system: domain, action: 'business_write', expected: tc === 99 || tc === 97 ? 'eventually_occur' : 'not_occur', operation_id_ref: id, details: { environment: 'mock' } }]
    : [{ system: 'business', action: 'state_change', expected: 'not_occur', operation_id_ref: null, details: {} }];
  const state = { review_status: null, return_status: null, funds_status: null };
  if (tc === 97) state.return_status = 'partially_inspected';
  if (tc === 98 || tc === 103) state.funds_status = 'not_executed';
  if (tc === 99) state.funds_status = 'succeeded_only_after_authoritative_confirmation';
  if (tc === 100) state.funds_status = 'unknown_until_query';
  const evidenceRefs = evidence ? evidence.split('|') : [];
  const expected = {
    assertion_mode: 'structured_facts_tool_calls_effects_evidence_state',
    outcome_label: expectedStatus,
    facts: factsByTc[tc].map(([factPath, operator, value]) => ({ path: factPath, operator, value })),
    tool_calls: { must: [{ tool_name: tool, arguments: toolArgs(tc) }], may: [], must_not: [{ tool_name: 'payment.execute_real' }] },
    effects,
    evidence_refs: evidenceRefs,
    forbidden: ['cross_tenant_disclosure', 'unverified_root_cause', 'real_payment_or_warehouse_side_effect'],
    state,
    reference_time: referenceTime,
    notes: `${expectedNote}；断言针对独立模拟适配器和固定快照，未实现的工具/副作用须报告为未执行。`
  };
  return {
    schema_version: '2.0', dataset_version: version, case_id: id, requirement_ids: fr.split('|'),
    tc_id: `TC-${String(tc).padStart(2,'0')}`, domain, tenant_id: 'tenant-north', actor: actorForTc(tc),
    input: { prompt }, setup: { reset_to_seed: true, fault_injection: tc === 100 ? 'commit_then_timeout' : tc === 101 ? 'invalid_callback' : null, overrides: [] },
    seed_refs: { systems_file: 'data/mock/v2/systems.json', snapshot_ids: evidenceRefs.map((ref)=>evidenceMap.get(ref)?.snapshot_id).filter(Boolean), evidence_refs: evidenceRefs },
    expected, synthetic_only: true
  };
});
const knowledgeCalls = {
  'TC-41': { prompt: '查一下净化器A空气净化器A的商品说明', tool_name: 'search_knowledge_base', arguments: { query: 'air purifier A air purifer A 净化器A', top_k: 5 } },
  'TC-124': { prompt: '用商品别名和拼写错误查找净化器A的商品说明', tool_name: 'search_knowledge_base', arguments: { query: 'air purifer A 净化器A 空气净化器A', top_k: 5 } },
  'TC-125': { prompt: '查询净化器A兼容的滤芯F-01', tool_name: 'query_knowledge_graph', arguments: { entity_names: ['Air purifier A', 'Filter F-01'], relation_types: ['COMPATIBLE_WITH'], limit: 10 } },
  'TC-126': { prompt: '查询2026年10月19日有效的退货政策', tool_name: 'get_policy_detail', arguments: { policy_id: 'POL-RETURN-01', as_of: referenceTime } },
  'TC-127': { prompt: '查询南区租户的退货政策', tool_name: 'search_knowledge_base', arguments: { query: 'South return policy', top_k: 5 } },
  'TC-128': { prompt: '查找等离子传送器滤芯兼容信息', tool_name: 'search_knowledge_base', arguments: { query: 'plasma teleporter filter compatibility', top_k: 5 } }
};
for (const row of scenarios) {
  const call = knowledgeCalls[row.tc_id];
  if (!call) continue;
  row.input.prompt = call.prompt;
  if (!row.actor.scopes.includes('knowledge:read')) row.actor.scopes.push('knowledge:read');
  row.expected.tool_calls.must = [{ tool_name: call.tool_name, arguments: call.arguments }];
}

const variants = [
  { tc:95, suffix:'unmapped', prompt:'商品编码没有有效映射时能否进入产品排名？', fault:'mapping_missing', changes:{'mapping.status':'unmapped','mapping.publishable':false} },
  { tc:95, suffix:'expired', prompt:'来源映射已过期时查询历史退货事实', fault:'mapping_expired', changes:{'mapping.status':'expired','mapping.effective_at_event_time':false} },
  { tc:96, suffix:'ambiguous', prompt:'同一租户同一来源编码命中两个有效商品映射', fault:'mapping_ambiguous', changes:{'mapping.status':'ambiguous','authorization.allowed':false} },
  { tc:97, suffix:'duplicate-receipt', prompt:'相同仓库收货幂等键重放两次', fault:'duplicate_receipt', changes:{'warehouse.duplicate_receipt_count':1,'warehouse.received_quantity_effective':10} },
  { tc:97, suffix:'overage', prompt:'实际收货数量超过退货授权量', fault:'receipt_overage', changes:{'warehouse.overage_accepted':false,'warehouse.receipt_status':'quarantined'} },
  { tc:98, suffix:'no-return-required', prompt:'已批准仅退款且策略明确无需实物退回', fault:'return_not_required_authorized', changes:{'payment.prerequisite_satisfied':true,'payment.allowed':true} },
  { tc:99, suffix:'missing-confirmation', prompt:'缺少客户绑定确认凭证时尝试退款执行', fault:'missing_confirmation', changes:{'payment.refund.status':'not_executed','payment.execution.allowed':false} },
  { tc:100, suffix:'restart-recovery', prompt:'渠道已受理但进程重启，按原operation_id恢复', fault:'restart_after_commit', changes:{'payment.refund.operation_count':1,'payment.refund.recovery_operation_id_reused':true} },
  { tc:101, suffix:'duplicate-callback', prompt:'同一个有效成功回调重复送达', fault:'duplicate_valid_callback', changes:{'payment.callback.duplicate_count':1,'payment.ledger.posting_count':1} },
  { tc:102, suffix:'over-ceiling', prompt:'并发部分退款合计超过原支付可退余额', fault:'refund_ceiling_race', changes:{'payment.refund.ceiling_guard':'blocked','payment.ledger.posting_count':0} },
  { tc:103, suffix:'stale-revision', prompt:'退款审批版本过期后仍尝试执行', fault:'stale_approval_revision', changes:{'payment.execution.allowed':false,'payment.funds_status':'not_executed'} },
  { tc:104, suffix:'late-arrival', prompt:'本期窗口内发生但源事件迟到超过水位', fault:'late_source_event', changes:{'metric.quality':'provisional','metric.ranking_allowed':false} },
  { tc:105, suffix:'zero-denominator', prompt:'某产品观察窗口内分母为零', fault:'zero_denominator', changes:{'metric.zero_denominator_value':null,'metric.rankable':false} },
  { tc:106, suffix:'stale-watermark', prompt:'退款源水位未覆盖完整统计窗口', fault:'stale_source_watermark', changes:{'metric.quality':'incomplete','metric.ranking_blocked':true} },
  { tc:107, suffix:'cross-team', prompt:'主管尝试下钻到团队授权范围之外的客户明细', fault:'out_of_scope_detail', changes:{'authorization.filter_applied_before_aggregate':true,'authorization.out_of_scope_details_returned':false} },
  { tc:110, suffix:'empty-baseline', prompt:'退货率基准期分母为零时调查异常', fault:'empty_baseline', changes:{'comparison.comparable':false,'investigation.confirmed_anomaly_claim':false} },
  { tc:112, suffix:'confirmed-reason', prompt:'仓储已确认的结构化质检原因可纳入因素分解', fault:'confirmed_reason_code', changes:{'investigation.confirmed_reason_code_required':true,'investigation.unconfirmed_reason_included':false} }
];
for (const variant of variants) {
  const base = scenarios.find((row) => row.tc_id === `TC-${String(variant.tc).padStart(2,'0')}`);
  const row = structuredClone(base);
  row.case_id = `v2-tc-${String(variant.tc).padStart(2,'0')}-${variant.suffix}`;
  row.variant_of = base.case_id;
  row.input.prompt = variant.prompt;
  row.setup.fault_injection = variant.fault;
  row.setup.overrides = [{ tenant_id: row.tenant_id, overlay_id: `overlay-${variant.suffix}`, changes: variant.changes }];
  row.expected.outcome_label = variant.fault;
  for (const fact of row.expected.facts) if (Object.hasOwn(variant.changes, fact.path)) fact.value = variant.changes[fact.path];
  for (const [factPath, value] of Object.entries(variant.changes)) if (!row.expected.facts.some((fact) => fact.path === factPath)) row.expected.facts.push({ path: factPath, operator: 'equals', value });
  row.expected.notes = `同一TC的独立边界变体；测试执行器必须在租户隔离overlay上执行并报告未执行状态。${variant.fault}`;
  scenarios.push(row);
}

// Create design fixtures for every requirement TC that does not yet have a hand-authored scenario.
// Inputs and acceptance criteria are copied from the authoritative requirements table; they are
// explicitly not represented as executed assertions until an application harness exists.
const acceptanceSource = fs.readFileSync(path.join(projectRoot, 'docs/需求详细规格.md'), 'utf8');
const acceptanceRows = new Map();
for (const line of acceptanceSource.split(/\r?\n/)) {
  const match = line.match(/^\|\s*(TC-\d+[A-Z]?)\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|\s*$/);
  if (match) acceptanceRows.set(match[1], { input: match[2], criteria: match[3] });
}
const requirementForTc = (tc) => {
  if ([1,10,40,42,43,44,45,46,47,48,49,50,51,52,53,54,55,56,65,66,67,68].includes(tc)) return ['FR-04'];
  if (tc===12) return ['FR-10'];
  if (tc===13) return ['FR-12'];
  if (tc===15) return ['FR-01'];
  if ([16,17,19,83,85].includes(tc)) return ['FR-13'];
  if (tc===20) return ['FR-09','FR-12'];
  if ([22,23,24,25,26,27,28,29,30,34,35,36,39].includes(tc)) return ['FR-11'];
  if ([37,38].includes(tc)||String(tc).startsWith('38')) return ['FR-17'];
  if ([58,59,60,61,62,63,64].includes(tc)) return ['FR-18'];
  if (tc===79) return ['FR-09'];
  if ([80,81].includes(tc)) return ['FR-08'];
  if (tc===82) return ['FR-10','FR-17'];
  if (tc===84) return ['FR-04'];
  if (tc===86) return ['FR-19'];
  if (tc===1||tc===10) return ['FR-04'];
  return ['FR-01'];
};
const operationForTc = new Map([
  [1,'knowledge.search'],[10,'knowledge.search'],[12,'agent.execute_bounded_loop'],[13,'model.invoke'],[15,'gateway.validate_input'],
  [16,'event.publish_ticket_created'],[17,'event.publish_refund_confirmed'],[19,'event.consume_idempotently'],[20,'event.handle_tool_call_failure'],
  [22,'memory.read_session'],[23,'memory.expire_session'],[24,'memory.read_session_across_instances'],[25,'memory.check_store_health'],
  [26,'memory.write_long_term_fact'],[27,'memory.read_long_term_fact'],[28,'memory.restore_long_term_fact'],[29,'memory.authorize_subject'],[30,'memory.handle_store_unavailable'],
  [34,'memory.summarize_conversation'],[35,'memory.enforce_context_budget'],[36,'memory.extract_preference'],[37,'checkpoint.restore'],[38,'checkpoint.handle_save_failure'],
  [39,'memory.apply_importance_decay'],[40,'knowledge.search'],[42,'knowledge.deduplicate_chunks'],[43,'knowledge.rank_hybrid_candidates'],[44,'knowledge.abstain_below_threshold'],
  [45,'knowledge.update_indexes'],[46,'knowledge.reject_partial_index_release'],[47,'knowledge.query_graph'],[48,'knowledge.search_policy'],[49,'knowledge.search_hybrid'],[50,'knowledge.reflect_version_conflict'],
  [51,'knowledge.degrade_to_rag'],[52,'knowledge.handle_graph_unavailable'],[53,'knowledge.execute_parameterized_query'],[54,'supervisor.route_business_query'],[55,'ontology.publish_evidenced_relation'],[56,'ontology.reject_relation_without_evidence'],
  [58,'address.require_fresh_confirmation'],[59,'address.check_order_authorization'],[60,'address.enforce_idempotency'],[61,'address.check_order_revision'],[62,'address.validate_delivery_fields'],[63,'address.recover_operation'],[64,'security.redact_address_data'],
  [65,'knowledge.list_registered_tools'],[66,'knowledge.search_dual_index'],[67,'knowledge.search_hybrid_and_graph'],[68,'knowledge.resolve_policy_version'],
  [79,'security.enforce_tenant_composite_keys'],[80,'ticket.enforce_confirmation_revision'],[81,'ticket.create_idempotent_fallback'],[82,'worker.enforce_fencing_token'],[83,'event.deduplicate_business_operation'],[84,'knowledge.activate_complete_release'],[85,'outbox.recover_delivery'],[86,'workbench.authorize_customer_context']
]);
const domainForTc = (tc) => {
  if (tc===1||tc===10||[40,42,43,44,45,46,47,48,49,50,51,52,53,54,55,56,65,66,67,68].includes(tc)) return 'knowledge';
  if ([22,23,24,25,26,27,28,29,30,34,35,36,39].includes(tc)) return 'memory';
  if ([37,38].includes(tc)||String(tc).startsWith('38')) return 'checkpoint';
  if ([58,59,60,61,62,63,64].includes(tc)) return 'address';
  if ([16,17,19,20,79,80,81,82,83,84,85,86].includes(tc)) return 'workflow';
  return 'agent';
};
const existingTcs = new Set(scenarios.map((row)=>row.tc_id));
for (const tcId of [...acceptanceRows.keys()].sort((a,b)=>a.localeCompare(b,undefined,{numeric:true}))) {
  if (existingTcs.has(tcId)) continue;
  const source = acceptanceRows.get(tcId);
  const suffix = tcId.slice(3);
  const tcNumber = Number(suffix.replace(/[A-Z]$/,''));
  const domain = domainForTc(tcNumber);
  const operation = operationForTc.get(tcNumber) || `acceptance.${tcId.toLowerCase().replace(/[^a-z0-9]+/g,'_')}`;
  const requester = [79,80,81,82,83,84,85,86].includes(tcNumber)
    ? { subject_id:'support-01', roles:['support'], scopes:['customer:read','ticket:read','ticket:write','handoff:accept'] }
    : { subject_id:'supervisor-01', roles:['supervisor'], scopes:['analytics:read','investigation:read','knowledge:read'] };
  const operationLayer = domain==='knowledge' && [55,56].includes(tcNumber) ? 'ontology_service' : 'agent_orchestration';
  const operationIdentity = operationLayer==='ontology_service' ? 'ontology_service' : 'supervisor_agent';
  const caseId = `v2-${tcId.toLowerCase().replace(/[^a-z0-9]+/g,'-')}`;
  scenarios.push({
    schema_version:'2.0', dataset_version:version, case_id:caseId, requirement_ids:requirementForTc(tcNumber), tc_id:tcId,
    domain, tenant_id:'tenant-north', actor:requester,
    input:{ prompt:source.input }, setup:{ reset_to_seed:true, fault_injection:null, overrides:[] },
    seed_refs:{ systems_file:'data/mock/v2/systems.json', snapshot_ids:[], evidence_refs:[] },
    expected:{
      assertion_mode:'structured_facts_tool_calls_effects_evidence_state', outcome_label:'design_fixture', facts:[], acceptance_criteria:[source.criteria],
      tool_calls:{ must:[], may:[], must_not:[] },
      effects:[{ system:'business', action:'state_change', expected:'not_occur', operation_id_ref:null, details:{} }],
      evidence_refs:[], forbidden:['cross_tenant_disclosure','unverified_root_cause','real_payment_or_warehouse_side_effect'],
      state:{ review_status:null, return_status:null, funds_status:null }, reference_time:referenceTime,
      notes:'从需求详细规格逐字提取的测试输入和验收标准；此设计夹具未执行，应用测试须实现具体断言后才能计入验收。',
      workflow_steps:{ must:[{ operation_id:operation, execution_layer:operationLayer, identity_ref:operationIdentity, assertion:'required_path' }], may:[] }
    },
    synthetic_only:true,
    identity_context:{ requester_ref:'requester', execution_identities:{ requester:{ identity_type:'human', ...requester }, supervisor_agent:{ identity_type:'service', subject_id:'supervisor-agent-01', roles:['after_sales_agent'], scopes:['analytics:read','investigation:read','knowledge:read'] }, ...(operationIdentity==='ontology_service'?{ontology_service:{identity_type:'service',subject_id:'ontology-service-01',roles:['ontology_publisher'],scopes:['ontology:publish','ontology:validate']}}:{}) } },
    partition:'development'
  });
}
const holdoutTc = new Set(['TC-04','TC-09','TC-18','TC-38','TC-96','TC-101','TC-106','TC-110','TC-112','TC-114','TC-117','TC-120','TC-122','TC-123']);
for (const row of scenarios) {
  const registeredMcpTools = new Set(['search_knowledge_base','query_knowledge_graph','get_policy_detail']);
  const workflow = [];
  const must = [];
  for (const call of row.expected.tool_calls.must || []) {
    if (registeredMcpTools.has(call.tool_name) && call.arguments && !Object.hasOwn(call.arguments,'tc_id')) {
      must.push({ ...call, identity_ref: 'requester' });
      continue;
    }
    const operation = call.tool_name;
    let execution_layer = 'application_use_case';
    let identity_ref = 'supervisor_agent';
    if (operation.startsWith('warehouse.')) { execution_layer = 'external_system_api'; identity_ref = 'warehouse_operator'; }
    else if (operation.startsWith('payment.')) { execution_layer = 'external_system_api'; identity_ref = 'payment_service'; }
    else if (/^(workflow|memory|checkpoint|event|dependency|gateway|mcp|workbench)\./.test(operation)) execution_layer = 'agent_orchestration';
    else if (/^(source|ontology)\./.test(operation)) execution_layer = 'ontology_service';
    workflow.push({ operation_id: operation, execution_layer, identity_ref, assertion: 'required_path' });
  }
  row.expected.tool_calls = { must, may: [], must_not: [] };
  row.expected.workflow_steps = { must: workflow, may: [] };
  const requester = row.actor;
  const execution_identities = { requester: { identity_type: 'human', subject_id: requester.subject_id, roles: requester.roles, scopes: requester.scopes } };
  if (workflow.some((step)=>step.identity_ref==='supervisor_agent')) execution_identities.supervisor_agent = { identity_type: 'service', subject_id: 'supervisor-agent-01', roles: ['after_sales_agent'], scopes: ['analytics:read','investigation:read'] };
  if (workflow.some((step)=>step.identity_ref==='warehouse_operator')) execution_identities.warehouse_operator = { identity_type: 'human_employee', subject_id: 'warehouse-employee-01', roles: ['warehouse_employee'], scopes: ['warehouse:receive','warehouse:inspect'] };
  if (workflow.some((step)=>step.identity_ref==='payment_service')) execution_identities.payment_service = { identity_type: 'service', subject_id: 'payment-service-01', roles: ['payment_executor'], scopes: ['payment:execute','payment:callback:verify'] };
  row.identity_context = { requester_ref: 'requester', execution_identities };
}
for (const row of scenarios) row.partition = holdoutTc.has(row.tc_id) ? 'holdout' : 'development';
write('scenarios/v2/multi_system.jsonl', scenarios.map((row) => JSON.stringify(row)).join('\n') + '\n');
write('scenarios/v2/catalog.json', { schema_version: '2.0', dataset_version: version, total_cases: scenarios.length, requirement_tc_count: acceptanceRows.size, mapped_tc_count: new Set(scenarios.map((row)=>row.tc_id)).size, development_cases: scenarios.filter((row)=>row.partition==='development').length, holdout_cases: scenarios.filter((row)=>row.partition==='holdout').length, holdout_is_blind: false, tc_range: 'TC-01..TC-128 (all requirement TCs mapped)', execution_readiness: 'design_only', execution_blockers: ['No application source code or scenario harness is present in the repository.','Design fixtures copied from requirement criteria are not executable acceptance tests until each criterion is bound to runtime assertions.'], scenario_contract: { mcp_tool_calls: 'Only registered MCP tools with real arguments and requester scopes.', workflow_steps: 'Application, orchestration, ontology, WMS and payment operations use distinct execution identities.', acceptance_criteria: 'Verbatim requirement criteria accompany every TC; criteria are not counted as passed runtime assertions.', execution_readiness_rule: 'Keep design_only until a harness executes scenarios against the application and system adapters.' }, scenario_file: 'data/scenarios/v2/multi_system.jsonl' });
const csv = ['requirement_id,tc_id,case_id,dataset_version,expected_schema_version,owner_role,status'];
const owners = { 'FR-20':'商品/数据负责人','FR-21':'仓储负责人','FR-22':'财务/支付负责人','FR-23':'售后主管/数据负责人','FR-24':'售后/运维负责人','FR-02':'Agent负责人','FR-03':'Agent负责人','FR-14':'Agent负责人','FR-15':'Agent负责人','FR-16':'Agent负责人' };
for (const c of scenarios) for (const fr of c.requirement_ids) csv.push([fr,c.tc_id,c.case_id,version,'2.0',owners[fr] || '项目负责人','generated'].join(','));
write('scenarios/v2/traceability.csv', csv.join('\n') + '\n');

const acceptanceTcs = [...new Set([...acceptanceSource.matchAll(/^\|\s*(TC-\d+[A-Z]?)\s*\|/gm)].map((match)=>match[1]))].sort((a,b)=>a.localeCompare(b, undefined, { numeric: true }));
const casesByTc = new Map();
for (const scenario of scenarios) {
  const list = casesByTc.get(scenario.tc_id) || [];
  list.push(scenario.case_id);
  casesByTc.set(scenario.tc_id, list);
}
const acceptanceMap = ['tc_id,scenario_status,case_ids,planned_test_id,planned_test_file,execution_status'];
for (const tcId of acceptanceTcs) {
  const slug = tcId.toLowerCase().replace('-', '_');
  const caseIds = casesByTc.get(tcId) || [];
  acceptanceMap.push([tcId,caseIds.length ? 'scenario_defined' : 'scenario_missing',[...caseIds].sort().join('|'),`AT-${tcId}`,`tests/acceptance/test_${slug}.py`,'not_implemented'].join(','));
}
write('scenarios/v2/acceptance-test-map.csv', acceptanceMap.join('\n') + '\n');

const files = [];
const walk = (dir) => fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => entry.isDirectory() ? walk(path.join(dir, entry.name)) : [path.join(dir, entry.name)]);
for (const folder of ['mock/v2','knowledge/v2','scenarios/v2','contracts']) for (const absolute of walk(path.join(dataRoot, folder))) {
  const relative = path.relative(projectRoot, absolute).split(path.sep).join('/');
  const body = fs.readFileSync(absolute);
  files.push({ path: relative, sha256: crypto.createHash('sha256').update(body).digest('hex'), bytes: body.length });
}
{
  const absolute = path.join(dataRoot, 'scenarios/expected-v2.schema.json');
  const relative = path.relative(projectRoot, absolute).split(path.sep).join('/');
  const body = fs.readFileSync(absolute);
  files.push({ path: relative, sha256: crypto.createHash('sha256').update(body).digest('hex'), bytes: body.length });
}
files.sort((a,b) => a.path.localeCompare(b.path));
const businessRecordCollections = ['products','product_mappings','orders','order_lines','shipment_batches','return_authorizations','warehouse_receipts','warehouse_inspections','payments','payment_details','refund_details','refund_executions','metric_snapshots','events','traces','anomaly_evidence'];
const businessRecordCounts = Object.fromEntries(businessRecordCollections.map((name)=>[name,systems[name].length]));
const businessRecordTotal = Object.values(businessRecordCounts).reduce((sum,count)=>sum+count,0);
write('manifests/mock-v2.json', { schema_version: '2.0', dataset_version: version, reference_time: referenceTime, synthetic_only: true, seed: 20261019, counts: { scenarios: scenarios.length, requirements: new Set(scenarios.flatMap(c => c.requirement_ids)).size, acceptance_tcs: acceptanceTcs.length, synthetic_order_bundles: systems.synthetic_batch.generated_orders, synthetic_products: systems.synthetic_batch.products, business_records: businessRecordTotal, business_record_counts: businessRecordCounts, mock_fact_files: 1, source_systems: ['OMS','PIM','PROJECTION','PAYMENT','WMS'], ontology_files: 4 }, files, note: '仅供离线模拟开发与数据契约验证；1000条synthetic order bundles包含订单及关联跨系统事实，business_records为各业务数组记录数之和；source_systems表示事实来源命名空间，不代表独立运行进程；验收映射中的not_implemented表示尚无应用运行时测试。' });
console.log(JSON.stringify({ dataset_version: version, scenarios: scenarios.length, requirements: new Set(scenarios.flatMap(c => c.requirement_ids)).size, synthetic_orders: systems.synthetic_batch.generated_orders, business_records: businessRecordTotal, files: files.length }));
