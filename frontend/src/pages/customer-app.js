import { me } from '../api/client.js';
import { loadSession } from '../auth/session.js';
import { mountAddressPage } from './order_address/page.js';
import { mountRefundPage } from './refund/page.js';
import { mountWorkbenchPage } from './workbench/page.js';

const chatPanel = document.getElementById('chat-panel');
const pageRoot = document.getElementById('page-root');
const feedback = document.getElementById('route-feedback');
const links = [...document.querySelectorAll('[data-route]')];
let session = loadSession();
let renderVersion = 0;
let cleanupPage = null;
const staffRoles = ['support', 'human_agent', 'supervisor'];

function updateWorkbenchNavigation(roles = session?.roles || []) {
  const workbenchLink = links.find(link => link.dataset.route === 'workbench');
  if (workbenchLink) workbenchLink.hidden = !roles.some(role => staffRoles.includes(role));
}

function currentRoute() {
  const route = location.hash.replace(/^#\/?/, '');
  return ['refund', 'order-address', 'workbench'].includes(route) ? route : 'chat';
}

function setActive(route) {
  for (const link of links) {
    const active = link.dataset.route === route;
    link.classList.toggle('active', active);
    if (active) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  }
}

async function render() {
  const version = ++renderVersion;
  cleanupPage?.(); cleanupPage = null;
  const route = currentRoute();
  document.body.classList.remove('agent-workbench-active');
  setActive(route);
  feedback.hidden = true;
  if (route === 'chat') {
    updateWorkbenchNavigation();
    pageRoot.hidden = true; chatPanel.hidden = !session?.token;
    return;
  }
  chatPanel.hidden = true; pageRoot.hidden = false;
  if (!session?.token) {
    pageRoot.replaceChildren(); feedback.textContent = '请先在左侧登录客户账号，再使用退款和地址确认。';
    feedback.hidden = false; return;
  }
  try {
    const identity = await me(session.token);
    if (version !== renderVersion) return;
    session.roles = identity.data.roles || [];
    session.subjectId = identity.data.subject_id;
    session.tenantId = identity.data.tenant_id;
    updateWorkbenchNavigation(session.roles);
    if (route === 'workbench') {
      if (!session.roles.some(role => staffRoles.includes(role))) {
        pageRoot.replaceChildren();
        const message = document.createElement('div');
        message.className = 'feedback feedback-error';
        message.setAttribute('role', 'alert');
        message.textContent = '当前账号没有客服工作台权限。';
        pageRoot.append(message); return;
      }
      document.body.classList.add('agent-workbench-active');
      cleanupPage = mountWorkbenchPage(pageRoot, session, identity.data);
      return;
    }
    if (!session.roles.includes('customer')) {
      pageRoot.replaceChildren();
      const message = document.createElement('div');
      message.className = 'feedback feedback-error';
      message.setAttribute('role', 'alert');
      message.textContent = '此处仅供客户本人确认操作。请切换到客户本人账号。';
      pageRoot.append(message); return;
    }
    if (route === 'refund') cleanupPage = mountRefundPage(pageRoot, session);
    else cleanupPage = mountAddressPage(pageRoot, session);
  } catch (error) {
    if (version !== renderVersion) return;
    pageRoot.replaceChildren();
    const message = document.createElement('div');
    message.className = 'feedback feedback-error';
    message.setAttribute('role', 'alert');
    message.textContent = error.status === 401
      ? '登录已失效，请重新登录后继续。'
      : '暂时无法验证客户身份，请检查连接后重试。';
    pageRoot.append(message);
  }
}

window.addEventListener('hashchange', render);
window.addEventListener('customer:session-changed', () => { session = loadSession(); render(); });
if (!location.hash) location.hash = '#/chat';
else render();
