-- MySQL 8.0+ contract baseline for the first cross-system vertical slice.
-- Tenant IDs are part of every business key and lookup index. Apply only through versioned migrations.
-- Runtime code, migration tooling and integration tests are not present in this docs/data-only baseline.

CREATE TABLE ontology_release (
  tenant_id VARCHAR(64) NOT NULL,
  release_id VARCHAR(96) NOT NULL,
  schema_version VARCHAR(32) NOT NULL,
  manifest_digest CHAR(64) NOT NULL,
  status ENUM('draft','validated','active','retired','rejected') NOT NULL,
  revision BIGINT UNSIGNED NOT NULL,
  activated_at DATETIME(6) NULL,
  active_slot TINYINT GENERATED ALWAYS AS (CASE WHEN status = 'active' THEN 1 ELSE NULL END) STORED,
  created_at DATETIME(6) NOT NULL,
  PRIMARY KEY (tenant_id, release_id),
  UNIQUE KEY uq_ontology_active (tenant_id, active_slot),
  CHECK (revision > 0)
) ENGINE=InnoDB;

CREATE TABLE entity_mapping (
  tenant_id VARCHAR(64) NOT NULL,
  source_system VARCHAR(64) NOT NULL,
  entity_type VARCHAR(64) NOT NULL,
  source_id VARCHAR(160) NOT NULL,
  canonical_id VARCHAR(160) NOT NULL,
  mapping_version BIGINT UNSIGNED NOT NULL,
  valid_from DATETIME(6) NOT NULL,
  valid_to DATETIME(6) NULL,
  evidence_ref VARCHAR(192) NOT NULL,
  status ENUM('active','ambiguous','rejected','retired') NOT NULL,
  PRIMARY KEY (tenant_id, source_system, entity_type, source_id, mapping_version),
  KEY ix_mapping_canonical (tenant_id, entity_type, canonical_id, valid_from, valid_to),
  CHECK (valid_to IS NULL OR valid_to > valid_from)
) ENGINE=InnoDB;

CREATE TABLE return_order (
  tenant_id VARCHAR(64) NOT NULL,
  return_id VARCHAR(96) NOT NULL,
  request_id VARCHAR(96) NOT NULL,
  order_id VARCHAR(96) NOT NULL,
  status ENUM('authorized','receiving','inspection_pending','partially_inspected','completed','rejected','cancelled') NOT NULL,
  overage_tolerance INT UNSIGNED NOT NULL DEFAULT 0,
  revision BIGINT UNSIGNED NOT NULL,
  created_at DATETIME(6) NOT NULL,
  updated_at DATETIME(6) NOT NULL,
  PRIMARY KEY (tenant_id, return_id),
  UNIQUE KEY uq_return_request (tenant_id, request_id),
  KEY ix_return_order (tenant_id, order_id),
  CHECK (revision > 0)
) ENGINE=InnoDB;

CREATE TABLE return_line (
  tenant_id VARCHAR(64) NOT NULL,
  return_line_id VARCHAR(96) NOT NULL,
  return_id VARCHAR(96) NOT NULL,
  order_line_id VARCHAR(96) NOT NULL,
  canonical_product_id VARCHAR(160) NOT NULL,
  sku_id VARCHAR(160) NOT NULL,
  lot_number VARCHAR(96) NULL,
  serial_number VARCHAR(160) NULL,
  authorized_quantity INT UNSIGNED NOT NULL,
  overage_tolerance INT UNSIGNED NOT NULL DEFAULT 0,
  received_quantity INT UNSIGNED NOT NULL DEFAULT 0,
  accepted_quantity INT UNSIGNED NOT NULL DEFAULT 0,
  rejected_quantity INT UNSIGNED NOT NULL DEFAULT 0,
  revision BIGINT UNSIGNED NOT NULL,
  PRIMARY KEY (tenant_id, return_line_id),
  KEY ix_return_line_parent (tenant_id, return_id),
  CONSTRAINT fk_return_line_parent FOREIGN KEY (tenant_id, return_id) REFERENCES return_order(tenant_id, return_id),
  CHECK (accepted_quantity + rejected_quantity <= received_quantity),
  CHECK (received_quantity <= authorized_quantity + overage_tolerance)
) ENGINE=InnoDB;

CREATE TABLE warehouse_operation (
  tenant_id VARCHAR(64) NOT NULL,
  operation_id VARCHAR(96) NOT NULL,
  return_line_id VARCHAR(96) NOT NULL,
  operation_type ENUM('receipt','inspection','disposition') NOT NULL,
  quantity INT UNSIGNED NOT NULL,
  disposition ENUM('restock','quarantine','scrap','return_to_supplier','pending_review') NULL,
  source_record_id VARCHAR(160) NULL,
  actor_id VARCHAR(96) NOT NULL,
  idempotency_key VARCHAR(128) NOT NULL,
  expected_revision BIGINT UNSIGNED NOT NULL,
  created_at DATETIME(6) NOT NULL,
  PRIMARY KEY (tenant_id, operation_id),
  UNIQUE KEY uq_warehouse_idempotency (tenant_id, idempotency_key),
  KEY ix_warehouse_line (tenant_id, return_line_id, created_at)
) ENGINE=InnoDB;

CREATE TABLE inventory_disposition_posting (
  tenant_id VARCHAR(64) NOT NULL,
  posting_id VARCHAR(96) NOT NULL,
  inspection_operation_id VARCHAR(96) NOT NULL,
  return_line_id VARCHAR(96) NOT NULL,
  disposition ENUM('restock','quarantine','scrap','return_to_supplier') NOT NULL,
  quantity INT UNSIGNED NOT NULL,
  warehouse_location VARCHAR(96) NULL,
  inventory_transaction_id VARCHAR(160) NOT NULL,
  actor_id VARCHAR(96) NOT NULL,
  idempotency_key VARCHAR(128) NOT NULL,
  posted_at DATETIME(6) NOT NULL,
  PRIMARY KEY (tenant_id, posting_id),
  UNIQUE KEY uq_inventory_transaction (tenant_id, inventory_transaction_id),
  UNIQUE KEY uq_inventory_disposition_idempotency (tenant_id, idempotency_key)
) ENGINE=InnoDB;

CREATE TABLE payment_channel_policy (
  tenant_id VARCHAR(64) NOT NULL,
  channel_id VARCHAR(64) NOT NULL,
  policy_version BIGINT UNSIGNED NOT NULL,
  refund_deadline_days INT UNSIGNED NOT NULL,
  max_partial_refunds INT UNSIGNED NOT NULL,
  fee_treatment ENUM('merchant_absorbs','customer_prohibited','channel_defined','separate_contract') NOT NULL,
  fx_treatment ENUM('original_currency_only','channel_defined','manual_review') NOT NULL,
  chargeback_owner VARCHAR(96) NOT NULL,
  reconciliation_sla_hours INT UNSIGNED NOT NULL,
  effective_from DATETIME(6) NOT NULL,
  effective_to DATETIME(6) NULL,
  approval_reference VARCHAR(192) NOT NULL,
  status ENUM('draft','approved','retired') NOT NULL,
  PRIMARY KEY (tenant_id, channel_id, policy_version),
  CHECK (effective_to IS NULL OR effective_to > effective_from)
) ENGINE=InnoDB;

CREATE TABLE refund_execution (
  tenant_id VARCHAR(64) NOT NULL,
  execution_id VARCHAR(96) NOT NULL,
  refund_request_id VARCHAR(96) NOT NULL,
  original_payment_id VARCHAR(160) NOT NULL,
  amount_minor BIGINT UNSIGNED NOT NULL,
  currency CHAR(3) NOT NULL,
  funds_status ENUM('not_executed','queued','submitting','processing','unknown','succeeded','failed') NOT NULL,
  channel_reference VARCHAR(192) NULL,
  idempotency_key VARCHAR(128) NOT NULL,
  request_digest CHAR(64) NOT NULL,
  revision BIGINT UNSIGNED NOT NULL,
  created_at DATETIME(6) NOT NULL,
  updated_at DATETIME(6) NOT NULL,
  PRIMARY KEY (tenant_id, execution_id),
  UNIQUE KEY uq_refund_idempotency (tenant_id, idempotency_key),
  KEY ix_refund_request (tenant_id, refund_request_id),
  CHECK (amount_minor > 0),
  CHECK (CHAR_LENGTH(currency) = 3 AND currency = UPPER(currency))
) ENGINE=InnoDB;

CREATE TABLE refund_ledger (
  tenant_id VARCHAR(64) NOT NULL,
  ledger_id VARCHAR(96) NOT NULL,
  execution_id VARCHAR(96) NOT NULL,
  merchant_id VARCHAR(96) NOT NULL,
  channel_transaction_id VARCHAR(192) NOT NULL,
  amount_minor BIGINT UNSIGNED NOT NULL,
  currency CHAR(3) NOT NULL,
  evidence_ref VARCHAR(192) NOT NULL,
  confirmed_at DATETIME(6) NOT NULL,
  PRIMARY KEY (tenant_id, ledger_id),
  UNIQUE KEY uq_channel_refund (tenant_id, merchant_id, channel_transaction_id),
  UNIQUE KEY uq_ledger_execution (tenant_id, execution_id)
) ENGINE=InnoDB;

CREATE TABLE integration_outbox (
  tenant_id VARCHAR(64) NOT NULL,
  event_id VARCHAR(96) NOT NULL,
  event_type VARCHAR(128) NOT NULL,
  event_version INT UNSIGNED NOT NULL,
  operation_id VARCHAR(96) NULL,
  causation_id VARCHAR(96) NULL,
  parent_event_id VARCHAR(96) NULL,
  idempotency_key VARCHAR(128) NOT NULL,
  payload JSON NOT NULL,
  occurred_at DATETIME(6) NOT NULL,
  published_at DATETIME(6) NULL,
  retry_count INT UNSIGNED NOT NULL DEFAULT 0,
  PRIMARY KEY (tenant_id, event_id),
  UNIQUE KEY uq_outbox_idempotency (tenant_id, idempotency_key),
  KEY ix_outbox_pending (published_at, occurred_at)
) ENGINE=InnoDB;

CREATE TABLE agent_audit_log (
  tenant_id VARCHAR(64) NOT NULL,
  audit_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  event_time DATETIME(6) NOT NULL,
  actor_id VARCHAR(96) NOT NULL,
  action VARCHAR(128) NOT NULL,
  resource_type VARCHAR(64) NOT NULL,
  resource_id_hash CHAR(64) NOT NULL,
  request_id VARCHAR(96) NOT NULL,
  trace_id VARCHAR(64) NOT NULL,
  outcome VARCHAR(32) NOT NULL,
  redaction_profile VARCHAR(64) NOT NULL,
  previous_hash CHAR(64) NULL,
  record_hash CHAR(64) NOT NULL,
  PRIMARY KEY (tenant_id, audit_id),
  KEY ix_audit_trace (tenant_id, trace_id, event_time),
  KEY ix_audit_actor (tenant_id, actor_id, event_time)
) ENGINE=InnoDB;

CREATE TABLE after_sales_investigation (
  tenant_id VARCHAR(64) NOT NULL,
  investigation_id VARCHAR(96) NOT NULL,
  actor_id VARCHAR(96) NOT NULL,
  anomaly_type ENUM('business_metric','cross_system_conflict','runtime_failure') NOT NULL,
  snapshot_id VARCHAR(96) NOT NULL,
  scope_digest CHAR(64) NOT NULL,
  query_digest CHAR(64) NOT NULL,
  quality ENUM('complete','provisional','incomplete','blocked') NOT NULL,
  created_at DATETIME(6) NOT NULL,
  PRIMARY KEY (tenant_id, investigation_id),
  KEY ix_investigation_actor (tenant_id, actor_id, created_at)
) ENGINE=InnoDB;

CREATE TABLE investigation_revision (
  tenant_id VARCHAR(64) NOT NULL,
  investigation_id VARCHAR(96) NOT NULL,
  revision INT UNSIGNED NOT NULL,
  snapshot_id VARCHAR(96) NOT NULL,
  evidence_refs JSON NOT NULL,
  factors JSON NOT NULL,
  conclusion_status ENUM('candidate','unconfirmed','confirmed_by_human','superseded') NOT NULL,
  created_by VARCHAR(96) NOT NULL,
  created_at DATETIME(6) NOT NULL,
  PRIMARY KEY (tenant_id, investigation_id, revision),
  CONSTRAINT fk_investigation_revision FOREIGN KEY (tenant_id, investigation_id) REFERENCES after_sales_investigation(tenant_id, investigation_id)
) ENGINE=InnoDB;

-- Retention, legal hold, backup and WORM export are deployment policies; they must not be implemented as silent row deletion.
