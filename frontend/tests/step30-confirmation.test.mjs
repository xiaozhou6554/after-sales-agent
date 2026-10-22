import test from 'node:test';
import assert from 'node:assert/strict';
import {
  createRefundQuote, prepareRefundConfirmation, submitRefundRequest,
  prepareAddressChange, verifyAddressConfirmation, sendChat,
} from '../src/api/client.js';

const response = (status, body) => ({
  ok: status >= 200 && status < 300, status, json: async () => body,
});

test('退款报价和提交只发送页面收集的业务引用，确认由服务端验证', async () => {
  const calls = [];
  const fetcher = async (path, options) => {
    calls.push({ path, options });
    return response(201, { status: 'success', data: { quote_id: 'quote-1' } });
  };
  await createRefundQuote('customer-token', 'ORDER-001', 'changed_mind', [
    { order_line_id: 'LINE-001', quantity: 1 },
  ], fetcher);
  await prepareRefundConfirmation('customer-token', 'ORDER-001', 'quote-1', fetcher);
  await submitRefundRequest('customer-token', 'quote-1', 'confirm-1', fetcher);

  assert.equal(calls[0].path, '/orders/ORDER-001/refund-quotes');
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    schema_version: '1.0', reason_code: 'changed_mind',
    lines: [{ order_line_id: 'LINE-001', quantity: 1 }],
  });
  assert.equal(calls[1].path, '/orders/ORDER-001/refund-quotes/quote-1/confirm');
  assert.deepEqual(JSON.parse(calls[1].options.body), { schema_version: '1.0' });
  assert.deepEqual(JSON.parse(calls[2].options.body), {
    schema_version: '1.0', quote_id: 'quote-1', confirmation_id: 'confirm-1',
  });
  assert.ok(calls.every(call => call.options.headers.Authorization === 'Bearer customer-token'));
  assert.ok(calls.every(call => !('Idempotency-Key' in call.options.headers)));
});

test('完整地址只进入改址表单请求；验证接口不接收地址字段', async () => {
  const calls = [];
  const fetcher = async (path, options) => {
    calls.push({ path, options });
    return response(200, { status: 'success', data: {} });
  };
  const address = {
    receiver_name: '张女士', contact_phone: '13800138000', province: '浙江省',
    city: '杭州市', district: '西湖区', detail_address: '文三路 100 号 2 幢 301',
  };
  await prepareAddressChange('customer-token', 'ORDER-001', address, fetcher);
  await verifyAddressConfirmation('customer-token', 'confirm-2', '731204', fetcher);

  assert.equal(calls[0].path, '/orders/ORDER-001/shipping-address-changes');
  assert.deepEqual(JSON.parse(calls[0].options.body), { schema_version: '1.0', ...address });
  assert.equal(calls[1].path, '/shipping-address-confirmations/confirm-2/verify');
  assert.deepEqual(JSON.parse(calls[1].options.body), { schema_version: '1.0', challenge: '731204' });
  assert.equal(calls[1].options.headers.Authorization, 'Bearer customer-token');
});

test('回到对话的内容不会附带完整地址', async () => {
  let requestBody;
  await sendChat(
    'customer-token',
    '请应用订单 ORDER-001 的已确认修改 addr-123，地址摘要：张*，138****8000，浙江省杭州市西湖区********。',
    'thread-1', false, null,
    async (_path, options) => {
      requestBody = JSON.parse(options.body);
      return response(200, { status: 'success', answer: '已处理。' });
    },
  );
  assert.equal(requestBody.thread_id, 'thread-1');
  assert.ok(!JSON.stringify(requestBody).includes('13800138000'));
  assert.ok(!JSON.stringify(requestBody).includes('文三路 100 号'));
});
