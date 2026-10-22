import {
  createRefundQuote, prepareRefundConfirmation, recoverRefundRequest,
  submitRefundRequest, verifyRefundConfirmation,
} from '../../api/client.js';
import {
  button, customerOnly, errorMessage, feedback, field, formatDate,
  money, node, saveWorkflow, sectionHeading,
} from '../../components/confirmation.js';

const REASONS = [
  ['no_longer_needed', '不再需要'],
  ['changed_mind', '改变主意'],
  ['quality_issue', '商品质量问题'],
];

function readQuote(data) {
  return {
    quoteId: data.quote_id, orderId: data.order_id, amount: data.amount_minor,
    goods: data.goods_minor, shipping: data.shipping_minor, currency: data.currency,
    reason: data.reason_code, expiresAt: data.expires_at,
    orderVersion: data.order_version, paymentVersion: data.payment_version,
  };
}

function quoteCard(quote) {
  const card = node('section', 'quote-card');
  const total = node('div', 'quote-total');
  total.append(node('span', '', '本次服务端报价'), node('strong', '', money(quote.amount, quote.currency)));
  const details = node('dl', 'quote-details');
  const rows = [
    ['订单', quote.orderId], ['原因', REASONS.find(([code]) => code === quote.reason)?.[1] || quote.reason],
    ['商品金额', money(quote.goods, quote.currency)], ['运费', money(quote.shipping, quote.currency)],
    ['报价有效至', formatDate(quote.expiresAt)],
  ];
  for (const [label, value] of rows) {
    details.append(node('dt', '', label), node('dd', '', value));
  }
  card.append(total, details);
  return card;
}

export function mountRefundPage(root, session) {
  root.replaceChildren();
  if (!customerOnly(session)) {
    feedback(root, '退款确认仅向客户本人开放，请使用客户账号登录。', 'error');
    return;
  }

  const page = node('div', 'service-page');
  page.append(sectionHeading('申请退款', '先获取服务端报价，再核对金额并完成本人确认。提交后可在此查询审核状态。'));
  const status = node('div', 'page-feedback');
  const form = node('form', 'workflow-form');
  const order = field('订单编号', 'order_id', { autocomplete: 'off' });
  order.input.placeholder = '例如 ORDER-001';
  const reason = node('select', 'form-control');
  reason.name = 'reason_code'; reason.required = true;
  reason.append(node('option', '', '请选择退款原因'));
  reason.options[0].value = '';
  for (const [code, label] of REASONS) {
    const option = node('option', '', label); option.value = code; reason.append(option);
  }
  const reasonField = node('label', 'form-field');
  reasonField.append(node('span', '', '退款原因'), reason);
  const line = field('订单商品行编号', 'order_line_id');
  line.input.placeholder = '请填写订单详情中的商品行编号';
  const quantity = field('申请数量', 'quantity', { type: 'number', inputMode: 'numeric' });
  quantity.input.min = '1'; quantity.input.value = '1';
  const quoteButton = button('获取报价', 'primary-button', 'submit');
  form.append(order.wrapper, reasonField, line.wrapper, quantity.wrapper, quoteButton);
  const quoteArea = node('div', 'quote-area');
  const confirmationArea = node('div', 'confirmation-area');
  page.append(status, form, quoteArea, confirmationArea);
  root.append(page);

  let currentQuote = null;
  let confirmation = null;
  let busy = false;
  const workflow = session.workflow || (session.workflow = {});

  const setBusy = value => {
    busy = value; quoteButton.disabled = value;
    form.querySelectorAll('input,select').forEach(input => { input.disabled = value; });
  };
  const showRequest = data => {
    confirmationArea.replaceChildren();
    const block = node('section', 'result-card');
    const states = {
      pending_review: ['已提交，等待审核', '申请已提交，退款尚未执行。'],
      reviewing: ['正在审核', '审核仍在进行，退款尚未执行。'],
      approved: ['审核已通过', data.funds_status === 'succeeded'
        ? '服务端已确认资金退回。' : '审核通过不代表资金已退回；请稍后查询退款状态。'],
      rejected: ['申请未通过', '如需继续，请根据审核结果重新发起申请。'],
      unknown: ['结果待核实', '暂时无法确认提交结果。请使用下方按钮查询原申请，不要重复提交。'],
    };
    const [title, detail] = states[data.review_status] || ['申请状态已更新', `当前状态：${data.review_status || '待核实'}`];
    block.append(node('h2', '', title), node('p', '', detail));
    if (data.refund_request_id) block.append(node('p', 'reference-line', `申请编号：${data.refund_request_id}`));
    if (data.amount_minor != null) block.append(node('p', 'reference-line', `服务端确认金额：${money(data.amount_minor, data.currency || currentQuote?.currency || 'CNY')}`));
    if (data.funds_status) {
      const funds = data.funds_status === 'succeeded' ? '服务端确认已退款'
        : data.funds_status === 'failed' ? '退款执行失败'
          : '资金结果尚未确认';
      block.append(node('p', 'reference-line', funds));
    }
    const query = button('查询原申请状态', 'secondary-light-button');
    query.addEventListener('click', async () => {
      query.disabled = true;
      try {
        const result = await recoverRefundRequest(session.token, workflow.refundConfirmationId);
        showRequest(result.data);
      } catch (error) {
        feedback(status, errorMessage(error, '暂时无法读取申请状态，请稍后查询。'), error.status === 0 ? 'warning' : 'error');
      } finally { query.disabled = false; }
    });
    block.append(query); confirmationArea.append(block);
  };

  async function recoverExisting() {
    if (!workflow.refundConfirmationId) return;
    try {
      const result = await recoverRefundRequest(session.token, workflow.refundConfirmationId);
      showRequest(result.data);
      feedback(status, '已恢复原申请状态。', 'success');
    } catch (error) {
      if (error.status === 404) {
        if (workflow.refundVerified && workflow.refundQuoteId) {
          feedback(status, '本人确认已完成，但尚未找到已提交申请。可安全重试同一确认，服务端会按原确认编号去重。', 'warning');
          const retry = button('使用原确认重试提交', 'primary-button');
          retry.addEventListener('click', submitVerifiedQuote); confirmationArea.append(retry);
        } else {
          feedback(status, '已找到本次确认编号，但尚无已提交的退款申请。请查询状态或重新获取报价。', 'warning');
        }
      } else {
        feedback(status, errorMessage(error), error.status === 0 ? 'warning' : 'error');
      }
    }
  }

  async function submitVerifiedQuote() {
    if (busy || !workflow.refundVerified || !workflow.refundQuoteId || !workflow.refundConfirmationId) return;
    busy = true;
    feedback(status, '正在查询并恢复原确认对应的提交…');
    try {
      const submitted = await submitRefundRequest(
        session.token, workflow.refundQuoteId, workflow.refundConfirmationId,
      );
      workflow.refundVerified = false; saveWorkflow(session);
      showRequest(submitted.data);
      feedback(status, '已使用同一确认完成提交；服务端按原确认编号保证幂等。', 'success');
    } catch (error) {
      try {
        const recovered = await recoverRefundRequest(session.token, workflow.refundConfirmationId);
        workflow.refundVerified = false; saveWorkflow(session);
        showRequest(recovered.data);
        feedback(status, '已从服务端恢复原申请结果，没有创建重复申请。', 'success');
      } catch {
        feedback(status, errorMessage(error, '暂时无法确认原提交结果，请查询原申请状态。'), 'warning');
      }
    } finally { busy = false; }
  }

  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (busy) return;
    setBusy(true); feedback(status, '正在向服务端获取报价…');
    quoteArea.replaceChildren(); confirmationArea.replaceChildren(); currentQuote = null; confirmation = null;
    try {
      const result = await createRefundQuote(
        session.token, order.input.value.trim(), reason.value,
        [{ order_line_id: line.input.value.trim(), quantity: Number(quantity.input.value) }],
      );
      if (result.status !== 'success' || !result.data?.quote_id) {
        feedback(status, result.answer || '当前条件无法生成自动报价，请联系人工客服。', 'warning');
        return;
      }
      currentQuote = readQuote(result.data);
      quoteArea.append(quoteCard(currentQuote));
      const prepare = button('核对报价并开始本人验证', 'primary-button');
      confirmationArea.append(prepare);
      feedback(status, '报价由服务端计算；确认前不会提交退款申请。', 'success');
      prepare.addEventListener('click', async () => {
        if (busy || !currentQuote) return;
        busy = true; prepare.disabled = true;
        try {
          const prepared = await prepareRefundConfirmation(session.token, currentQuote.orderId, currentQuote.quoteId);
          confirmation = prepared.data;
          workflow.refundConfirmationId = confirmation.confirmation_id;
          workflow.refundQuoteId = currentQuote.quoteId;
          workflow.refundOrderId = currentQuote.orderId;
          saveWorkflow(session);
          const verifyForm = node('form', 'verification-form');
          verifyForm.append(node('p', 'verification-copy', `请完成一次性验证。有效期至 ${formatDate(confirmation.expires_at)}。`));
          if (confirmation.mock_challenge) verifyForm.append(node('p', 'mock-code', `本地模拟验证码：${confirmation.mock_challenge}`));
          const challenge = field('输入验证信息', 'challenge', { maxLength: 128 });
          const verify = button('验证本人并提交申请', 'primary-button', 'submit');
          verifyForm.append(challenge.wrapper, verify); confirmationArea.replaceChildren(verifyForm);
          verifyForm.addEventListener('submit', async verifyEvent => {
            verifyEvent.preventDefault();
            if (busy) return;
            busy = true; verify.disabled = true;
            try {
              await verifyRefundConfirmation(session.token, confirmation.confirmation_id, challenge.input.value.trim());
              workflow.refundVerified = true; saveWorkflow(session);
              feedback(status, '本人验证完成，正在提交已确认的报价…', 'success');
              const submitted = await submitRefundRequest(session.token, currentQuote.quoteId, confirmation.confirmation_id);
              workflow.refundVerified = false; saveWorkflow(session);
              showRequest(submitted.data);
            } catch (error) {
              if (error.status === 0 || error.status >= 500) {
                try {
                  const recovered = await recoverRefundRequest(session.token, confirmation.confirmation_id);
                  workflow.refundVerified = false; saveWorkflow(session);
                  showRequest(recovered.data);
                  feedback(status, '已从服务端恢复原申请结果，没有重复提交。', 'success');
                } catch {
                  feedback(status, '提交结果暂时未知。请查询原申请状态；确认前不要重新发起。', 'warning');
                  const retry = button('查询原申请状态', 'secondary-light-button');
                  retry.addEventListener('click', recoverExisting); confirmationArea.append(retry);
                  const safeRetry = button('使用原确认重试提交', 'primary-button');
                  workflow.refundVerified = true; saveWorkflow(session);
                  safeRetry.addEventListener('click', submitVerifiedQuote); confirmationArea.append(safeRetry);
                }
              } else {
                feedback(status, errorMessage(error), error.status === 409 ? 'warning' : 'error');
                if (error.status === 409 || error.code === 'CONFIRMATION_INVALID') {
                  workflow.refundVerified = false; saveWorkflow(session);
                  quoteArea.replaceChildren(); confirmationArea.replaceChildren(); currentQuote = null; confirmation = null;
                } else verify.disabled = false;
              }
            } finally { busy = false; }
          });
        } catch (error) {
          feedback(status, errorMessage(error, '无法开始验证，请重新获取报价。'), error.status === 0 ? 'warning' : 'error');
          prepare.disabled = false;
        } finally { busy = false; }
      });
    } catch (error) {
      const expired = error.status === 409 || error.code === 'STATE_CONFLICT';
      feedback(status, expired ? '订单或报价版本已变化。请重新获取服务端报价。' : errorMessage(error), error.status === 0 ? 'warning' : 'error');
    } finally { setBusy(false); }
  });

  recoverExisting();
}
