import {
  prepareAddressChange, queryAddressChange, verifyAddressConfirmation,
} from '../../api/client.js';
import {
  button, customerOnly, errorMessage, feedback, field, formatDate,
  node, saveWorkflow, sectionHeading,
} from '../../components/confirmation.js';

function addressPrompt(orderId, changeId, maskedSummary) {
  return `我已在独立的地址确认表单中完成本人验证。请应用收货地址修改：订单号 ${orderId}，地址修改编号 ${changeId}。已确认地址摘要：${maskedSummary}。请不要询问或复述地址原文。`;
}

export function mountAddressPage(root, session) {
  root.replaceChildren();
  if (!customerOnly(session)) {
    feedback(root, '收货地址确认仅向客户本人开放，请使用客户账号登录。', 'error');
    return () => {};
  }

  const page = node('div', 'service-page');
  page.append(sectionHeading('修改收货地址', '地址信息通过独立表单安全提交。验证后只把订单编号和脱敏摘要带回服务对话。'));
  const status = node('div', 'page-feedback');
  const privacy = node('aside', 'privacy-note', '完整地址不会写入聊天消息或浏览器会话。刷新后需重新填写未完成的表单。');
  const form = node('form', 'workflow-form address-form');
  const order = field('订单编号', 'order_id'); order.input.placeholder = '例如 ORDER-001';
  const receiver = field('收件人', 'receiver_name', { autocomplete: 'name', maxLength: 64 });
  const phone = field('联系电话', 'contact_phone', { type: 'tel', inputMode: 'tel', autocomplete: 'tel', maxLength: 32 });
  const province = field('省份', 'province', { maxLength: 32 });
  const city = field('城市', 'city', { maxLength: 32 });
  const district = field('区 / 县', 'district', { maxLength: 32 });
  const detail = field('街道、门牌及其他地址', 'detail_address', { multiline: true, maxLength: 128, autocomplete: 'street-address' });
  const prepare = button('提交地址并获取验证', 'primary-button', 'submit');
  form.append(order.wrapper, receiver.wrapper, phone.wrapper, province.wrapper, city.wrapper, district.wrapper, detail.wrapper, prepare);
  const summaryArea = node('div', 'summary-area');
  const confirmationArea = node('div', 'confirmation-area');
  page.append(status, privacy, form, summaryArea, confirmationArea);
  root.append(page);

  const workflow = session.workflow || (session.workflow = {});
  let current = null;
  let busy = false;

  const setFormBusy = value => {
    busy = value; form.querySelectorAll('input,textarea,button').forEach(input => { input.disabled = value; });
  };
  function showChange(data) {
    const box = node('section', 'result-card');
    const stateNames = {
      pending: workflow.addressApplyAttempted
        ? ['结果待核实', '申请仍显示待处理。请查询后端状态，再决定是否继续，避免重复提交。']
        : workflow.addressVerified
          ? ['等待应用', '本人验证已完成。订单编号和脱敏摘要可用于服务对话。']
          : ['等待确认', '地址修改尚未完成。请重新填写完整地址并完成本人验证。'],
      succeeded: ['地址已更新', '服务端已确认地址修改完成。'],
      failed: ['修改未完成', '服务端记录了失败状态。请核对订单状态后重新发起。'],
      unknown: ['结果待核实', '服务端暂时无法确认修改结果。请先查询原申请状态。'],
    };
    const [title, message] = stateNames[data.status] || ['地址修改状态', `当前状态：${data.status || '待核实'}`];
    box.append(node('h2', '', title), node('p', '', message));
    box.append(node('p', 'reference-line', `订单编号：${data.order_id || workflow.addressOrderId}`));
    box.append(node('p', 'reference-line', `地址修改编号：${data.address_change_id || workflow.addressChangeId}`));
    const query = button('重新查询状态', 'secondary-light-button');
    query.addEventListener('click', refreshStatus); box.append(query);
    if (data.status !== 'succeeded' && workflow.addressVerified && data.status !== 'failed') {
      const apply = button(workflow.addressApplyAttempted ? '在对话中再次查询并继续' : '在对话中应用已确认地址', 'primary-button');
      apply.addEventListener('click', () => {
        const prompt = addressPrompt(workflow.addressOrderId, workflow.addressChangeId, workflow.addressMaskedSummary);
        workflow.addressApplyAttempted = true;
        saveWorkflow(session);
        window.dispatchEvent(new CustomEvent('customer:send-prompt', { detail: { message: prompt } }));
      });
      box.append(apply);
    }
    summaryArea.replaceChildren(box);
  }

  async function refreshStatus() {
    if (!workflow.addressChangeId || busy) return;
    busy = true; feedback(status, '正在查询原修改单…');
    try {
      const result = await queryAddressChange(session.token, workflow.addressChangeId);
      showChange(result.data);
      feedback(status, '状态已从服务端更新。', 'success');
    } catch (error) {
      feedback(status, errorMessage(error, '暂时无法查询地址修改状态。'), error.status === 0 ? 'warning' : 'error');
    } finally { busy = false; }
  }

  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (busy) return;
    setFormBusy(true); feedback(status, '地址内容仅用于本次受控表单请求…');
    confirmationArea.replaceChildren(); summaryArea.replaceChildren(); current = null;
    try {
      const address = {
        receiver_name: receiver.input.value.trim(), contact_phone: phone.input.value.trim(),
        province: province.input.value.trim(), city: city.input.value.trim(),
        district: district.input.value.trim(), detail_address: detail.input.value.trim(),
      };
      const result = await prepareAddressChange(session.token, order.input.value.trim(), address);
      if (result.status !== 'success' || !result.data?.address_change_id) {
        feedback(status, result.answer || '当前订单暂不支持修改地址，请联系人工客服。', 'warning');
        return;
      }
      current = result.data;
      workflow.addressChangeId = current.address_change_id;
      workflow.addressConfirmationId = current.confirmation_id;
      workflow.addressOrderId = current.order_id;
      workflow.addressMaskedSummary = current.masked_summary;
      workflow.addressVerified = false;
      workflow.addressApplyAttempted = false;
      saveWorkflow(session);
      for (const item of [receiver, phone, province, city, district, detail]) item.input.value = '';
      const summary = node('section', 'quote-card address-summary');
      summary.append(node('h2', '', '请核对将要修改的地址'), node('p', '', current.masked_summary));
      summary.append(node('p', 'reference-line', `订单编号：${current.order_id}`));
      summary.append(node('p', 'reference-line', `有效期至：${formatDate(current.expires_at)}`));
      summaryArea.append(summary);

      const verifyForm = node('form', 'verification-form');
      verifyForm.append(node('p', 'verification-copy', '只有客户本人完成验证后，才能继续应用此修改。'));
      if (current.mock_challenge) verifyForm.append(node('p', 'mock-code', `本地模拟验证码：${current.mock_challenge}`));
      const challenge = field('输入验证信息', 'challenge', { maxLength: 128 });
      const verify = button('验证本人', 'primary-button', 'submit');
      verifyForm.append(challenge.wrapper, verify); confirmationArea.replaceChildren(verifyForm);
      feedback(status, '地址已加密登记，当前仅等待本人验证；尚未修改订单。', 'success');
      verifyForm.addEventListener('submit', async verifyEvent => {
        verifyEvent.preventDefault();
        if (busy || !current) return;
        busy = true; verify.disabled = true;
        try {
          await verifyAddressConfirmation(session.token, current.confirmation_id, challenge.input.value.trim());
          workflow.addressVerified = true; saveWorkflow(session);
          feedback(status, '本人验证完成。下一步会将订单编号与脱敏摘要发到服务对话，不会发送完整地址。', 'success');
          showChange({
            address_change_id: current.address_change_id, order_id: current.order_id,
            status: 'pending',
          });
          confirmationArea.replaceChildren();
        } catch (error) {
          feedback(status, errorMessage(error, '验证失败，请检查验证码或重新提交地址。'), error.status === 0 ? 'warning' : 'error');
          verify.disabled = false;
          if (error.code === 'CONFIRMATION_INVALID' || error.status === 409) {
            workflow.addressVerified = false; saveWorkflow(session);
            confirmationArea.replaceChildren();
          }
        } finally { busy = false; }
      });
    } catch (error) {
      feedback(status, errorMessage(error, '无法提交地址。请核对订单状态后重试。'), error.status === 0 ? 'warning' : 'error');
    } finally { setFormBusy(false); }
  });

  if (workflow.addressChangeId) refreshStatus();
  return () => {
    for (const item of [receiver, phone, province, city, district, detail]) item.input.value = '';
  };
}
