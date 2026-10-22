// 用户和服务端文本只用 textContent 渲染，防止来源内容注入 HTML。
export function appendMessage(container, role, text, sources = []) {
  const item = document.createElement('article');
  item.className = `message ${role}`;
  const meta = document.createElement('div');
  meta.className = 'message-meta';
  meta.textContent = role === 'user' ? '我' : '售后服务';
  const body = document.createElement('div');
  body.textContent = text;
  item.append(meta, body);
  if (sources.length) {
    const box = document.createElement('div');
    box.className = 'sources';
    box.textContent = '来源';
    const list = document.createElement('ul');
    for (const source of sources) {
      const entry = document.createElement('li');
      entry.textContent = source.title || source.document_id || source.source_ref || '已核验来源';
      list.append(entry);
    }
    box.append(list);
    item.append(box);
  }
  container.append(item);
  container.scrollTop = container.scrollHeight;
}
