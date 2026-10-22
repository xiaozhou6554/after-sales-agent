// 统一解析后端信封；401 清理登录态，其他错误仅显示受控消息。
export class ApiError extends Error {
  constructor(message, status, code = null) { super(message); this.status = status; this.code = code; }
}

export async function request(path, { token, method = 'GET', body, headers = {}, fetcher = fetch } = {}) {
  let response;
  try {
    response = await fetcher(path, {
      method,
      headers: {
        ...(body ? { 'Content-Type': 'application/json' } : {}),
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...headers,
      },
      ...(body ? { body: JSON.stringify(body) } : {}),
    });
  } catch {
    throw new ApiError('网络连接中断，请检查连接后重试。', 0);
  }
  let payload;
  try { payload = await response.json(); } catch { throw new ApiError('服务响应暂不可用。', response.status); }
  if (!response.ok || payload.status === 'error') {
    const message = response.status === 401
      ? '登录已失效，请重新登录。'
      : (payload.error?.message || '请求未完成，请稍后重试。');
    throw new ApiError(message, response.status, payload.error?.code || null);
  }
  return payload;
}

export const login = (accountId, fetcher) => request('/auth/mock/session', {
  method: 'POST', body: { schema_version: '1.0', account_id: accountId }, fetcher,
});
export const me = (token, fetcher) => request('/me', { token, fetcher });
export const sendChat = (token, message, threadId, asynchronous, key, fetcher, customerId = null) => request('/chat', {
  token, method: 'POST', fetcher,
  headers: asynchronous ? { Prefer: 'respond-async', 'Idempotency-Key': key } : {},
  body: { schema_version: '1.0', message, ...(threadId ? { thread_id: threadId } : {}),
    ...(customerId ? { customer_id: customerId } : {}) },
});
export const getTask = (token, taskId, fetcher) => request(`/tasks/${encodeURIComponent(taskId)}`, { token, fetcher });

export const createRefundQuote = (token, orderId, reasonCode, lines, fetcher) => request(
  `/orders/${encodeURIComponent(orderId)}/refund-quotes`, {
    token, method: 'POST', fetcher,
    body: { schema_version: '1.0', reason_code: reasonCode, lines },
  },
);
export const prepareRefundConfirmation = (token, orderId, quoteId, fetcher) => request(
  `/orders/${encodeURIComponent(orderId)}/refund-quotes/${encodeURIComponent(quoteId)}/confirm`,
  { token, method: 'POST', fetcher, body: { schema_version: '1.0' } },
);
export const verifyRefundConfirmation = (token, confirmationId, challenge, fetcher) => request(
  `/refund-confirmations/${encodeURIComponent(confirmationId)}/verify`, {
    token, method: 'POST', fetcher,
    body: { schema_version: '1.0', challenge },
  },
);
export const submitRefundRequest = (token, quoteId, confirmationId, fetcher) => request(
  '/refund-requests', {
    token, method: 'POST', fetcher,
    body: { schema_version: '1.0', quote_id: quoteId, confirmation_id: confirmationId },
  },
);
export const recoverRefundRequest = (token, confirmationId, fetcher) => request(
  `/refund-requests/by-confirmation/${encodeURIComponent(confirmationId)}`, { token, fetcher },
);
export const queryRefundRequest = (token, requestId, fetcher) => request(
  `/refund-requests/${encodeURIComponent(requestId)}`, { token, fetcher },
);

export const prepareAddressChange = (token, orderId, address, fetcher) => request(
  `/orders/${encodeURIComponent(orderId)}/shipping-address-changes`, {
    token, method: 'POST', fetcher,
    body: { schema_version: '1.0', ...address },
  },
);
export const verifyAddressConfirmation = (token, confirmationId, challenge, fetcher) => request(
  `/shipping-address-confirmations/${encodeURIComponent(confirmationId)}/verify`, {
    token, method: 'POST', fetcher,
    body: { schema_version: '1.0', challenge },
  },
);
export const queryAddressChange = (token, changeId, fetcher) => request(
  `/shipping-address-changes/${encodeURIComponent(changeId)}`, { token, fetcher },
);
export const recoverAddressChange = (token, confirmationId, fetcher) => request(
  `/shipping-address-changes/by-confirmation/${encodeURIComponent(confirmationId)}`, { token, fetcher },
);

export const listWorkbenchCustomers = (token, query = '', fetcher) => request(
  `/workbench/customers?query=${encodeURIComponent(query)}`, { token, fetcher },
);
export const listWorkbenchTickets = (token, customerId, fetcher) => request(
  `/workbench/tickets?${new URLSearchParams(customerId ? { customer_id: customerId } : {})}`, { token, fetcher },
);
export const getWorkbenchTicket = (token, ticketId, fetcher) => request(
  `/workbench/tickets/${encodeURIComponent(ticketId)}`, { token, fetcher },
);
export const listWorkbenchHandoffs = (token, fetcher) => request('/workbench/handoffs', { token, fetcher });
export const acceptWorkbenchHandoff = (token, handoffId, revision, key, fetcher) => request(
  `/workbench/handoffs/${encodeURIComponent(handoffId)}/accept`, {
    token, method: 'POST', fetcher, headers: { 'Idempotency-Key': key },
    body: { expected_revision: revision },
  },
);
export const listWorkbenchRefunds = (token, customerId, fetcher) => request(
  `/workbench/refund-requests?${new URLSearchParams(customerId ? { customer_id: customerId } : {})}`, { token, fetcher },
);
export const reviewWorkbenchRefund = (token, refundId, revision, decision, reason, key, fetcher) => request(
  `/workbench/refund-requests/${encodeURIComponent(refundId)}/review`, {
    token, method: 'POST', fetcher, headers: { 'Idempotency-Key': key },
    body: { schema_version: '1.0', expected_revision: revision, decision, review_reason: reason },
  },
);
export const prepareWorkbenchAssignment = (token, ticketId, assignedTo, revision, fetcher) => request(
  `/workbench/tickets/${encodeURIComponent(ticketId)}/assignment/confirmations`, {
    token, method: 'POST', fetcher,
    body: { schema_version: '1.0', assigned_to: assignedTo, expected_revision: revision },
  },
);
export const prepareWorkbenchStatus = (token, ticketId, targetStatus, reason, revision, fetcher) => request(
  `/workbench/tickets/${encodeURIComponent(ticketId)}/status/confirmations`, {
    token, method: 'POST', fetcher,
    body: { schema_version: '1.0', target_status: targetStatus, reason, expected_revision: revision },
  },
);
export const verifyWorkbenchTicketConfirmation = (token, confirmationId, challenge, fetcher) => request(
  `/workbench/ticket-confirmations/${encodeURIComponent(confirmationId)}/verify`, {
    token, method: 'POST', fetcher, body: { schema_version: '1.0', challenge },
  },
);
export const assignWorkbenchTicket = (token, ticketId, assignedTo, revision, confirmationId, key, fetcher) => request(
  `/workbench/tickets/${encodeURIComponent(ticketId)}/assignment`, {
    token, method: 'POST', fetcher, headers: { 'Idempotency-Key': key },
    body: { assigned_to: assignedTo, expected_revision: revision, confirmation_id: confirmationId },
  },
);
export const changeWorkbenchTicketStatus = (token, ticketId, targetStatus, reason, revision, confirmationId, key, fetcher) => request(
  `/workbench/tickets/${encodeURIComponent(ticketId)}/status`, {
    token, method: 'POST', fetcher, headers: { 'Idempotency-Key': key },
    body: { target_status: targetStatus, reason, expected_revision: revision, confirmation_id: confirmationId },
  },
);
