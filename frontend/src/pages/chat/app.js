import { ApiError, getTask, login, me, sendChat } from '../../api/client.js';
import { clearSession, loadSession, saveSession } from '../../auth/session.js';
import { appendMessage } from '../../components/message.js';

const $ = id => document.getElementById(id);
const ui = {
  loginPanel: $('login-panel'), chatPanel: $('chat-panel'), loginForm: $('login-form'),
  loginError: $('login-error'), chatError: $('chat-error'), messages: $('messages'),
  composer: $('composer'), message: $('message'), send: $('send-button'),
  taskBanner: $('task-banner'), identity: $('identity-label'), connection: $('connection-label'),
  thread: $('thread-label'), logout: $('logout'), newThread: $('new-thread'), asyncMode: $('async-mode'),
};
const staffRoles = new Set(['support', 'human_agent', 'supervisor']);
let session = loadSession();
let pollTimer = null;

function persist() { if (session) saveSession(session); }
function errorTo(node, error) { node.textContent = error.message; node.hidden = false; }
function showLogin() {
  session = null; clearSession(); clearInterval(pollTimer); pollTimer = null;
  ui.loginPanel.hidden = false; ui.chatPanel.hidden = true; ui.logout.hidden = true;
  ui.identity.textContent = '未登录'; ui.connection.textContent = '未连接';
  ui.send.disabled = false; ui.newThread.disabled = false;
  window.dispatchEvent(new Event('customer:session-changed'));
}
function showChat() {
  ui.loginPanel.hidden = true; ui.chatPanel.hidden = false; ui.logout.hidden = false;
  ui.identity.textContent = session.subjectId;
  const staffNeedsCustomer = session.roles?.some(role => staffRoles.has(role)) && !session.customerId;
  ui.thread.textContent = session.threadId || (staffNeedsCustomer ? '请先选择服务客户' : '新会话');
  ui.connection.textContent = '已连接';
  if (!ui.messages.children.length) {
    const empty = document.createElement('div'); empty.className = 'empty-state';
    empty.textContent = session.threadId
      ? '会话已恢复，可继续追问。'
      : staffNeedsCustomer
        ? '请先进入客服工作台，选择授权客户后再开始代客咨询。'
        : '请描述您的售后问题。';
    ui.messages.append(empty);
  }
}
function clearEmpty() { ui.messages.querySelector('.empty-state')?.remove(); }
function handleError(error, node) {
  if (error instanceof ApiError && error.status === 401) { showLogin(); errorTo(ui.loginError, error); return; }
  errorTo(node, error);
  if (error instanceof ApiError && error.status === 0) ui.connection.textContent = '网络中断';
  else if (error instanceof ApiError && error.status >= 500) ui.connection.textContent = '服务异常';
}

async function pollTask() {
  if (!session?.taskId) return;
  try {
    const result = await getTask(session.token, session.taskId);
    const task = result.data?.task;
    if (!task) throw new ApiError('任务状态暂不可读。', 503);
    if (task.status === 'pending' || task.status === 'running') {
      ui.taskBanner.textContent = '正在处理，请稍候。'; ui.taskBanner.hidden = false;
      ui.send.disabled = true; ui.newThread.disabled = true;
      return;
    }
    clearInterval(pollTimer); pollTimer = null;
    session.taskId = null; persist(); ui.taskBanner.hidden = true;
    ui.send.disabled = false; ui.newThread.disabled = false;
    clearEmpty();
    appendMessage(ui.messages, 'assistant', task.status === 'succeeded'
      ? (task.answer || '处理已完成。') : '处理未完成，请稍后重试或联系人工客服。');
  } catch (error) { handleError(error, ui.chatError); clearInterval(pollTimer); pollTimer = null; }
}
function resumeTask() {
  if (!session?.taskId) return;
  ui.send.disabled = true; ui.newThread.disabled = true;
  pollTask();
  pollTimer = setInterval(pollTask, 2500);
}

async function messageDigest(message) {
  const payload = new TextEncoder().encode(`${session.threadId || ''}\n${message}`);
  const bytes = await crypto.subtle.digest('SHA-256', payload);
  return Array.from(new Uint8Array(bytes), byte => byte.toString(16).padStart(2, '0')).join('');
}

ui.loginForm.addEventListener('submit', async event => {
  event.preventDefault(); ui.loginError.hidden = true;
  try {
    const grant = await login($('account-id').value.trim());
    const identity = await me(grant.data.session_token);
    session = { token: grant.data.session_token, subjectId: identity.data.subject_id,
      tenantId: identity.data.tenant_id, roles: identity.data.roles || [],
      customerId: identity.data.roles?.includes('customer')
        && identity.data.data_scope?.customer_ids?.length === 1
        ? identity.data.data_scope.customer_ids[0] : null,
      threadId: null, taskId: null };
    persist(); ui.messages.replaceChildren(); showChat();
    window.dispatchEvent(new Event('customer:session-changed'));
  } catch (error) { handleError(error, ui.loginError); }
});
ui.composer.addEventListener('submit', async event => {
  event.preventDefault(); ui.chatError.hidden = true;
  const message = ui.message.value.trim();
  if (!message || !session) return;
  if (session.roles?.some(role => staffRoles.has(role)) && !session.customerId) {
    errorTo(ui.chatError, new Error('请先进入客服工作台，选择授权客户后再开始代客咨询。'));
    return;
  }
  ui.send.disabled = true;
  ui.newThread.disabled = true;
  const asynchronous = ui.asyncMode.checked;
  try {
    let key = null;
    if (asynchronous) {
      const digest = await messageDigest(message);
      key = session.retry?.digest === digest ? session.retry.key : crypto.randomUUID();
      session.retry = { digest, key }; persist();
    }
    const response = await sendChat(
      session.token, message, session.threadId, asynchronous, key, fetch, session.customerId || null,
    );
    session.retry = null;
    clearEmpty(); appendMessage(ui.messages, 'user', message); ui.message.value = '';
    if (response.thread_id) { session.threadId = response.thread_id; ui.thread.textContent = response.thread_id; }
    if (response.status === 'pending') {
      session.taskId = response.async_task_id; ui.taskBanner.textContent = '正在处理，请稍候。';
      ui.taskBanner.hidden = false; persist(); resumeTask();
    } else {
      appendMessage(ui.messages, 'assistant', response.answer, response.sources || []); persist();
    }
  } catch (error) { handleError(error, ui.chatError); }
  finally {
    ui.send.disabled = Boolean(session?.taskId);
    ui.newThread.disabled = Boolean(session?.taskId);
  }
});
ui.logout.addEventListener('click', showLogin);
ui.newThread.addEventListener('click', () => {
  if (!session) return;
  session.threadId = null; session.taskId = null; session.retry = null;
  persist(); clearInterval(pollTimer); pollTimer = null;
  ui.messages.replaceChildren(); ui.taskBanner.hidden = true; showChat();
});

if (!session?.token) showLogin();
else me(session.token).then(identity => {
  if (identity.data.subject_id !== session.subjectId || identity.data.tenant_id !== session.tenantId) {
    showLogin(); return;
  }
  session.roles = identity.data.roles || [];
  session.customerId = session.roles.includes('customer')
    && identity.data.data_scope?.customer_ids?.length === 1
    ? identity.data.data_scope.customer_ids[0] : null;
  persist();
  showChat(); resumeTask();
}).catch(error => handleError(error, ui.loginError));

window.addEventListener('customer:send-prompt', event => {
  const prompt = event.detail?.message;
  if (!prompt || !session?.token) return;
  location.hash = '#/chat';
  window.dispatchEvent(new HashChangeEvent('hashchange'));
  requestAnimationFrame(() => {
    ui.message.value = prompt;
    ui.composer.requestSubmit();
  });
});

window.addEventListener('customer:context-changed', () => {
  session = loadSession();
  ui.messages.replaceChildren();
  ui.message.value = '';
  ui.taskBanner.hidden = true;
  ui.chatError.hidden = true;
  const staffNeedsCustomer = session?.roles?.some(role => staffRoles.has(role)) && !session.customerId;
  ui.thread.textContent = session?.customerId ? `代客服务 · ${session.customerId}`
    : staffNeedsCustomer ? '请先选择服务客户' : '新会话';
  if (session?.token && staffNeedsCustomer) {
    const empty = document.createElement('div'); empty.className = 'empty-state';
    empty.textContent = '请先进入客服工作台，选择授权客户后再开始代客咨询。';
    ui.messages.append(empty);
  }
});
