"""由独立索引维护账号执行的固定 Neo4j 约束与索引；查询适配器不执行 DDL。"""

SCHEMA_STATEMENTS = (
    "CREATE CONSTRAINT product_tenant_id IF NOT EXISTS FOR (n:Product) "
    "REQUIRE (n.tenant_id, n.knowledge_release_id, n.product_id) IS UNIQUE",
    "CREATE CONSTRAINT policy_tenant_id IF NOT EXISTS FOR (n:Policy) "
    "REQUIRE (n.tenant_id, n.knowledge_release_id, n.policy_id) IS UNIQUE",
    "CREATE CONSTRAINT accessory_tenant_id IF NOT EXISTS FOR (n:Accessory) "
    "REQUIRE (n.tenant_id, n.knowledge_release_id, n.accessory_id) IS UNIQUE",
    "CREATE CONSTRAINT document_tenant_id IF NOT EXISTS FOR (n:KnowledgeDocument) "
    "REQUIRE (n.tenant_id, n.knowledge_release_id, n.document_id) IS UNIQUE",
    "CREATE INDEX product_tenant_model IF NOT EXISTS FOR (n:Product) ON (n.tenant_id, n.model)",
    "CREATE INDEX policy_tenant_name IF NOT EXISTS FOR (n:Policy) ON (n.tenant_id, n.name)",
)
