export function node(tag, className = '', text = '') {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text) item.textContent = text;
  return item;
}

export function field(labelText, name, options = {}) {
  const wrapper = node('label', 'form-field');
  wrapper.append(node('span', '', labelText));
  const input = node(options.multiline ? 'textarea' : 'input', 'form-control');
  input.name = name;
  input.autocomplete = options.autocomplete || 'off';
  input.required = options.required !== false;
  if (options.multiline) {
    input.rows = options.rows || 3;
    input.maxLength = options.maxLength || 128;
  } else {
    input.type = options.type || 'text';
    input.maxLength = options.maxLength || 64;
    if (options.inputMode) input.inputMode = options.inputMode;
    if (options.pattern) input.pattern = options.pattern;
  }
  wrapper.append(input);
  return { wrapper, input };
}

export function feedback(target, message, kind = 'info') {
  target.replaceChildren();
  if (!message) return;
  const box = node('div', `feedback feedback-${kind}`, message);
  box.setAttribute(kind === 'error' ? 'role' : 'role', kind === 'error' ? 'alert' : 'status');
  target.append(box);
}

export function button(text, className = 'primary-button', type = 'button') {
  const item = node('button', className, text);
  item.type = type;
  return item;
}

export function sectionHeading(title, description) {
  const header = node('header', 'page-heading');
  header.append(node('h1', '', title), node('p', '', description));
  return header;
}

export function money(minor, currency) {
  const amount = (Number(minor) / 100).toFixed(2);
  return `${currency} ${amount}`;
}

export function formatDate(value) {
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return '有效期以服务端为准';
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  }).format(date);
}

export function saveWorkflow(session) {
  if (session) sessionStorage.setItem('after-sales.customer.session.v1', JSON.stringify(session));
}

export function customerOnly(session) {
  return Boolean(session?.token && Array.isArray(session.roles) && session.roles.includes('customer'));
}

export function errorMessage(error, fallback) {
  if (error?.status === 401) return '登录已失效，请重新登录后继续。';
  if (error?.code === 'CONFIRMATION_INVALID') return '验证已过期或无效，请重新发起确认。';
  if (error?.code === 'STATE_CONFLICT' || error?.status === 409) {
    return '订单或申请状态已经变化，请查询当前状态后再继续。';
  }
  if (error?.status === 0) return '网络中断，操作结果可能尚未确认。请先查询原申请状态。';
  return fallback || error?.message || '操作未完成，请查询状态或重新发起。';
}
