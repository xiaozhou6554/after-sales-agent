import test from 'node:test';
import assert from 'node:assert/strict';
import {
  acceptWorkbenchHandoff, listWorkbenchCustomers, reviewWorkbenchRefund,
  sendChat,
} from '../src/api/client.js';

const response = body => ({ ok: true, status: 200, json: async () => body });

test('客服查询使用服务端范围参数并正确编码筛选值', async () => {
  let call;
  await listWorkbenchCustomers('staff-token', '客户 01', async (path, options) => {
    call = { path, options };
    return response({ status: 'success', data: { items: [] } });
  });
  assert.equal(call.path, '/workbench/customers?query=%E5%AE%A2%E6%88%B7%2001');
  assert.equal(call.options.headers.Authorization, 'Bearer staff-token');
});

test('员工接单与退款审核通过幂等键提交明确版本和决定', async () => {
  const calls = [];
  const fetcher = async (path, options) => {
    calls.push({ path, options });
    return response({ status: 'success', data: {} });
  };
  await acceptWorkbenchHandoff('staff-token', 'handoff-1', 4, 'accept-key', fetcher);
  await reviewWorkbenchRefund('staff-token', 'refund-1', 7, 'approved', 'MANUAL_REVIEW', 'review-key', fetcher);
  assert.equal(calls[0].options.headers['Idempotency-Key'], 'accept-key');
  assert.equal(JSON.parse(calls[0].options.body).expected_revision, 4);
  assert.equal(JSON.parse(calls[1].options.body).decision, 'approved');
  assert.equal(JSON.parse(calls[1].options.body).review_reason, 'MANUAL_REVIEW');
});

test('代客咨询将被选客户绑定到独立会话请求', async () => {
  let body;
  await sendChat('staff-token', '订单进度', null, false, null, async (_path, options) => {
    body = JSON.parse(options.body);
    return response({ status: 'success', answer: '已查询' });
  }, 'CUSTOMER-01');
  assert.equal(body.customer_id, 'CUSTOMER-01');
  assert.equal(body.thread_id, undefined);
});
