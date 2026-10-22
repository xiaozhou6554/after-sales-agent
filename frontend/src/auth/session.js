// 只保存当前浏览器标签页所需的会话引用，退出时清除。
const KEY = 'after-sales.customer.session.v1';
export function loadSession(storage = sessionStorage) {
  try { return JSON.parse(storage.getItem(KEY) || 'null'); } catch { return null; }
}
export function saveSession(value, storage = sessionStorage) {
  storage.setItem(KEY, JSON.stringify(value));
}
export function clearSession(storage = sessionStorage) { storage.removeItem(KEY); }
