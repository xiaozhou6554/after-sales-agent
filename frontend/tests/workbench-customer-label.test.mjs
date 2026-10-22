import assert from 'node:assert/strict';
import test from 'node:test';

import { formatAuthorizedCustomerLabel } from '../src/pages/workbench/page.js';

test('authorized customer option includes its identifier for disambiguation', () => {
  assert.equal(
    formatAuthorizedCustomerLabel({
      display_name_masked: '客户 CUS***',
      customer_id: 'CUSTOMER-01',
    }),
    '客户 CUS*** · CUSTOMER-01',
  );
});
