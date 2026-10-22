import {
  acceptWorkbenchHandoff, getWorkbenchTicket, listWorkbenchCustomers,
  listWorkbenchHandoffs, listWorkbenchRefunds, listWorkbenchTickets,
  reviewWorkbenchRefund, prepareWorkbenchAssignment, prepareWorkbenchStatus,
  verifyWorkbenchTicketConfirmation, assignWorkbenchTicket, changeWorkbenchTicketStatus,
} from '../../api/client.js';
import { saveSession } from '../../auth/session.js';

const el = (tag, text, className) => {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
};

export function mountWorkbenchPage(root, session, identity) {
  let customerId = null;
  let busy = false;
  const page = el('div', undefined, 'workbench-page');
  const heading = el('div', undefined, 'workbench-heading');
  heading.append(el('p', '员工专用 · 客户授权范围内', 'eyebrow'), el('h1', '客服工作台'));
  const nav = el('nav', undefined, 'agent-workbench-nav');
  nav.setAttribute('aria-label', '工作台导航');
  const conversationLink = el('a', '服务会话');
  conversationLink.href = '#/chat';
  const queueLink = el('a', '工单工作台');
  queueLink.href = '#/workbench';
  queueLink.setAttribute('aria-current', 'page');
  nav.append(conversationLink, queueLink);
  const presence = el('span', '坐席工作台', 'agent-presence');
  heading.append(nav, presence);
  const context = el('section', undefined, 'workbench-context');
  context.append(el('label', '当前授权客户', 'field-label'));
  const select = el('select');
  select.setAttribute('aria-label', '选择授权客户');
  select.append(new Option('选择客户', ''));
  context.append(select);
  const consult = el('button', '开始代客咨询', 'primary-button');
  consult.type = 'button'; consult.disabled = true;
  consult.addEventListener('click', () => { location.hash = '#/chat'; });
  context.append(consult);
  const status = el('p', '正在读取授权客户…', 'workbench-status');
  status.setAttribute('role', 'status');
  const layout = el('div', undefined, 'workbench-layout');
  const tickets = el('section', undefined, 'workbench-section workbench-queue');
  const ticketList = el('div', undefined, 'workbench-list');
  const ticketDetail = el('div', undefined, 'workbench-detail');
  const queueHeading = el('h2', '会话队列');
  tickets.append(queueHeading, ticketList);
  const conversation = el('section', undefined, 'workbench-conversation');
  conversation.append(el('h2', '服务记录'), ticketDetail);
  const inspector = el('aside', undefined, 'workbench-inspector');
  const agentSummary = el('section', undefined, 'workbench-agent-summary');
  agentSummary.append(el('h2', '当前服务依据'));
  const summaryContent = el('div', '选择队列中的工单，查看已授权的客户记录、状态和可用操作。', 'workbench-summary-content');
  agentSummary.append(summaryContent);
  const handoffs = el('section', undefined, 'workbench-section');
  const handoffList = el('div', undefined, 'workbench-list');
  handoffs.append(el('h2', '人工协作'), handoffList);
  const refunds = el('section', undefined, 'workbench-section');
  const refundList = el('div', undefined, 'workbench-list');
  refunds.append(el('h2', '退款审核'), refundList);
  inspector.append(agentSummary, handoffs, refunds);
  layout.append(tickets, conversation, inspector);
  page.append(heading, context, status, layout);
  root.replaceChildren(page);

  const note = value => { status.textContent = value; };
  const appendEmpty = (target, message) => target.append(el('p', message, 'workbench-empty'));
  const showConversationEmpty = (title, message) => {
    const empty = el('div', undefined, 'workbench-empty-panel');
    empty.append(el('span', '✦', 'workbench-empty-mark'), el('h3', title), el('p', message));
    ticketDetail.replaceChildren(empty);
  };
  appendEmpty(ticketList, '选择一个已授权客户，查看对应的服务队列。');
  showConversationEmpty('从服务队列开始', '选择客户后，这里会显示该客户已授权的工单记录和服务消息。');
  const button = (label, action, kind = 'secondary-button') => {
    const result = el('button', label, kind);
    result.type = 'button'; result.addEventListener('click', action); return result;
  };
  const appendConfirmation = (target, message, prepared, commit) => {
    const box = el('div', undefined, 'workbench-confirmation');
    box.append(el('p', message));
    if (prepared.data.challenge) box.append(el('p', `本地模拟验证码：${prepared.data.challenge}`, 'mock-code'));
    const label = el('label', '输入一次性确认码');
    const challenge = document.createElement('input'); challenge.required = true; challenge.maxLength = 128;
    label.append(challenge);
    const confirm = button('明确确认', async event => {
      event.preventDefault();
      if (!challenge.value.trim()) { challenge.focus(); return; }
      confirm.disabled = true;
      try {
        await verifyWorkbenchTicketConfirmation(session.token, prepared.data.confirmation_id, challenge.value.trim());
        await commit(prepared.data.confirmation_id);
      } catch (error) { note(error.message); confirm.disabled = false; }
    }, 'primary-button');
    box.append(label, confirm); target.append(box);
  };

  async function loadTicket(ticketId) {
    ticketDetail.replaceChildren(el('p', '正在读取工单…', 'workbench-empty'));
    try {
      const response = await getWorkbenchTicket(session.token, ticketId);
      const item = response.data;
      const facts = el('dl', undefined, 'workbench-facts');
      for (const [label, value] of [
        ['工单编号', item.ticket_id],
        ['处理状态', item.status],
        ['优先级', item.priority],
        ['记录版本', item.revision],
      ]) {
        facts.append(el('dt', label), el('dd', String(value)));
      }
      summaryContent.replaceChildren(el('strong', item.title), facts,
        el('p', '以上信息来自当前坐席已授权的工单记录。Agent 建议以会话和知识库实际返回内容为准。'));
      const detail = el('article', undefined, 'workbench-ticket-detail');
      detail.append(el('h3', item.title), el('p', `状态：${item.status} · 优先级：${item.priority} · 版本：${item.revision}`));
      detail.append(el('p', item.description_masked, 'workbench-description'));
      const messages = el('div', undefined, 'workbench-messages');
      for (const message of item.messages || []) {
        const row = el('article', undefined, `workbench-message visibility-${message.visibility}`);
        row.append(el('span', message.visibility === 'internal' ? '内部备注' : '客户可见'), el('p', message.content_masked));
        messages.append(row);
      }
      detail.append(messages);
      if (identity.scopes.includes('ticket:assign')) {
        const form = document.createElement('form'); form.className = 'workbench-actions';
        const target = document.createElement('input'); target.required = true; target.maxLength = 64;
        target.placeholder = '同团队员工 subject ID'; target.setAttribute('aria-label', '分派员工');
        const submit = el('button', '准备分派确认', 'secondary-button'); submit.type = 'submit';
        form.append(target, submit); form.addEventListener('submit', async event => {
          event.preventDefault();
          try {
            const prepared = await prepareWorkbenchAssignment(session.token, item.ticket_id, target.value.trim(), item.revision);
            appendConfirmation(detail, `确认将工单分派给 ${target.value.trim()}。`, prepared, async confirmationId => {
              await assignWorkbenchTicket(session.token, item.ticket_id, target.value.trim(), item.revision,
                confirmationId, crypto.randomUUID());
              note('工单已分派，正在刷新详情。'); await loadTickets(); await loadTicket(item.ticket_id);
            });
          } catch (error) { note(error.message); }
        });
        detail.append(el('h3', '分派工单'), form);
      }
      if (identity.scopes.includes('ticket:status')) {
        const form = document.createElement('form'); form.className = 'workbench-actions';
        const target = document.createElement('select'); target.setAttribute('aria-label', '目标工单状态');
        for (const value of ['in_progress', 'resolved', 'closed']) target.append(new Option(value, value));
        const reason = document.createElement('input'); reason.required = true; reason.maxLength = 1000;
        reason.value = 'CUSTOMER_REQUEST'; reason.setAttribute('aria-label', '状态变更原因');
        const submit = el('button', '准备状态变更', 'secondary-button'); submit.type = 'submit';
        form.append(target, reason, submit); form.addEventListener('submit', async event => {
          event.preventDefault();
          try {
            const prepared = await prepareWorkbenchStatus(session.token, item.ticket_id,
              target.value, reason.value.trim(), item.revision);
            appendConfirmation(detail, `确认将工单状态变更为 ${target.value}。`, prepared, async confirmationId => {
              await changeWorkbenchTicketStatus(session.token, item.ticket_id, target.value, reason.value.trim(),
                item.revision, confirmationId, crypto.randomUUID());
              note('工单状态已更新，正在刷新详情。'); await loadTickets(); await loadTicket(item.ticket_id);
            });
          } catch (error) { note(error.message); }
        });
        detail.append(el('h3', '更新状态'), form);
      }
      ticketDetail.replaceChildren(detail);
    } catch (error) { ticketDetail.replaceChildren(el('p', error.message, 'feedback feedback-error')); }
  }

  async function loadTickets() {
    ticketList.replaceChildren(); ticketDetail.replaceChildren();
    if (!customerId) {
      appendEmpty(ticketList, '先选择一个已授权客户。');
      showConversationEmpty('选择授权客户', '客户范围由服务端控制。选择客户后，工作台会重新查询对应服务记录。');
      return;
    }
    try {
      const response = await listWorkbenchTickets(session.token, customerId);
      for (const ticket of response.data.items || []) {
        const row = el('article', undefined, 'workbench-list-item');
        row.append(el('strong', `${ticket.ticket_id} · ${ticket.status}`), el('span', `优先级 ${ticket.priority} · 修订 ${ticket.revision}`));
        row.append(button('查看详情', () => loadTicket(ticket.ticket_id)));
        ticketList.append(row);
      }
      if (!ticketList.children.length) {
        appendEmpty(ticketList, '该客户暂无工单。');
        showConversationEmpty('暂无服务记录', '该客户当前没有可展示的工单。你仍可进入服务会话继续代客咨询。');
      }
    } catch (error) { appendEmpty(ticketList, error.message); }
  }

  async function loadHandoffs() {
    handoffList.replaceChildren();
    try {
      const response = await listWorkbenchHandoffs(session.token);
      for (const item of response.data.items || []) {
        const row = el('article', undefined, 'workbench-list-item');
        row.append(el('strong', `${item.handoff_id} · ${item.status}`), el('p', item.summary_masked));
        if (item.status === 'queued' && identity.scopes.includes('handoff:accept')) {
          row.append(button('接收请求', async event => {
            if (busy) return; busy = true; event.currentTarget.disabled = true;
            try {
              const key = `handoff-${item.handoff_id}-${item.revision}`;
              await acceptWorkbenchHandoff(session.token, item.handoff_id, item.revision, key);
              note('接单结果已记录，正在刷新队列。'); await loadHandoffs();
            } catch (error) { note(error.message); event.currentTarget.disabled = false; }
            finally { busy = false; }
          }, 'primary-button'));
        }
        handoffList.append(row);
      }
      if (!handoffList.children.length) appendEmpty(handoffList, '当前授权范围内没有队列项目。');
    } catch (error) { appendEmpty(handoffList, error.message); }
  }

  async function loadRefunds() {
    refundList.replaceChildren();
    if (!identity.scopes.includes('refund:review')) { appendEmpty(refundList, '当前账号没有退款审核权限。'); return; }
    try {
      const response = await listWorkbenchRefunds(session.token, customerId);
      for (const item of response.data.items || []) {
        const row = el('article', undefined, 'workbench-list-item');
        row.append(el('strong', `${item.refund_request_id} · ${item.status}`),
          el('span', `${(item.amount_minor / 100).toFixed(2)} ${item.currency} · 资金状态 ${item.funds_status}`));
        if (item.status === 'reviewing') {
          const reason = document.createElement('input'); reason.value = 'MANUAL_REVIEW';
          reason.setAttribute('aria-label', '退款审核原因代码');
          const actions = el('div', undefined, 'workbench-actions');
          for (const decision of ['approved', 'rejected']) {
            actions.append(button(decision === 'approved' ? '批准' : '拒绝', async event => {
              if (busy) return; busy = true; event.currentTarget.disabled = true;
              try {
                await reviewWorkbenchRefund(session.token, item.refund_request_id, item.revision,
                  decision, reason.value.trim() || 'MANUAL_REVIEW', `refund-${item.refund_request_id}-${item.revision}-${decision}`);
                note('审核决定已提交；这不代表资金已退回。'); await loadRefunds();
              } catch (error) { note(error.message); event.currentTarget.disabled = false; }
              finally { busy = false; }
            }, decision === 'approved' ? 'primary-button' : 'secondary-button'));
          }
          row.append(reason, actions);
        }
        refundList.append(row);
      }
      if (!refundList.children.length) appendEmpty(refundList, '当前授权范围内没有退款申请。');
    } catch (error) { appendEmpty(refundList, error.message); }
  }

  select.addEventListener('change', async () => {
    customerId = select.value || null;
    session.customerId = customerId;
    session.threadId = null;
    session.taskId = null;
    session.retry = null;
    saveSession(session);
    window.dispatchEvent(new Event('customer:context-changed'));
    consult.disabled = !customerId;
    ticketDetail.replaceChildren();
    note(customerId ? '已切换客户；正在重新加载授权数据。' : '请选择授权客户。');
    await Promise.all([loadTickets(), loadRefunds()]);
  });

  (async () => {
    try {
      if (!identity.scopes.includes('customer:read')) throw new Error('当前账号没有客户查询权限。');
      const response = await listWorkbenchCustomers(session.token);
      for (const item of response.data.items || []) select.append(new Option(item.display_name_masked, item.customer_id));
      note(response.data.items?.length ? '客户范围由服务端授权；切换后将重新查询工作台数据。' : '当前账号没有可显示的授权客户。');
      if (identity.scopes.includes('handoff:read')) await loadHandoffs();
      else appendEmpty(handoffList, '当前账号没有人工队列读取权限。');
      if (identity.scopes.includes('refund:review')) await loadRefunds();
      else appendEmpty(refundList, '当前账号没有退款审核权限。');
    } catch (error) { note(error.message); }
  })();

  return () => { busy = false; page.replaceChildren(); };
}
