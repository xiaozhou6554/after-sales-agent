import test from 'node:test';
import assert from 'node:assert/strict';
import { ApiError, getTask, login, sendChat } from '../src/api/client.js';

const response = (status, body) => ({ ok: status >= 200 && status < 300, status, json: async () => body });

test('Mock 登录仅提交预置账号 ID', async () => {
  let options;
  const result = await login('cust-north-01', async (_path, request) => {
    options = request;
    return response(200, { status: 'success', data: { session_token: 'test-token' } });
  });
  assert.equal(result.data.session_token, 'test-token');
  assert.deepEqual(JSON.parse(options.body), { schema_version: '1.0', account_id: 'cust-north-01' });
});

test('异步对话保留原幂等键，任务查询只使用原任务 ID', async () => {
  let chatRequest;
  await sendChat('test-token', '订单在哪里', 'thread-1', true, 'stable-key', async (path, options) => {
    chatRequest = { path, options };
    return response(202, { status: 'pending', async_task_id: 'task-1' });
  });
  assert.equal(chatRequest.options.headers['Idempotency-Key'], 'stable-key');
  assert.equal(chatRequest.options.headers.Prefer, 'respond-async');
  assert.equal(JSON.parse(chatRequest.options.body).thread_id, 'thread-1');
  let path;
  await getTask('test-token', 'task/1', async requested => {
    path = requested;
    return response(200, { status: 'pending', data: { task: { status: 'running' } } });
  });
  assert.equal(path, '/tasks/task%2F1');
});

test('401 和网络故障显示受控错误', async () => {
  await assert.rejects(
    () => getTask('t', 'task', async () => response(401, { status: 'error' })),
    error => error instanceof ApiError && error.status === 401 && /登录已失效/.test(error.message),
  );
  await assert.rejects(
    () => getTask('t', 'task', async () => { throw new Error('sensitive internal detail'); }),
    error => error instanceof ApiError && error.status === 0 && !error.message.includes('sensitive'),
  );
});
