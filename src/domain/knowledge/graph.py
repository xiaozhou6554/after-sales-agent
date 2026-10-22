"""一期知识关系类型、端点和业务标识的唯一白名单。"""

from __future__ import annotations

from datetime import date

NODE_IDS = {
    "Product": "product_id",
    "ProductCategory": "category_id",
    "ProductVersion": "version_id",
    "Accessory": "accessory_id",
    "Feature": "feature_id",
    "UsageScenario": "scenario_id",
    "FaultSymptom": "fault_id",
    "ServiceItem": "service_id",
    "Policy": "policy_id",
    "PolicyClause": "clause_id",
    "WarrantyRule": "warranty_id",
    "KnowledgeDocument": "document_id",
}

RELATION_ENDPOINTS = {
    "BELONGS_TO": ("Product", "ProductCategory"),
    "HAS_VERSION": ("Product", "ProductVersion"),
    "HAS_ACCESSORY": ("Product", "Accessory"),
    "COMPATIBLE_WITH": ("Product", "Accessory"),
    "NOT_COMPATIBLE_WITH": ("Product", "Accessory"),
    "HAS_FEATURE": ("Product", "Feature"),
    "SUITABLE_FOR": ("Product", "UsageScenario"),
    "HAS_FAULT_SYMPTOM": ("Product", "FaultSymptom"),
    "SUGGESTS_SERVICE": ("FaultSymptom", "ServiceItem"),
    "HAS_POLICY": ("Product", "Policy"),
    "APPLIES_TO": ("Policy", "ProductCategory"),
    "CONTAINS_CLAUSE": ("Policy", "PolicyClause"),
    "HAS_WARRANTY": ("Product", "WarrantyRule"),
    "SUPPORTED_BY": ("PolicyClause", "KnowledgeDocument"),
    "DESCRIBES": ("KnowledgeDocument", "Product"),
    "RELATED_TO": ("KnowledgeDocument", "Policy"),
}


def valid_interval(start: date, end: date, as_of: date) -> bool:
    """有效区间两端均包含；开放边界应使用显式哨兵日期。"""
    return start <= as_of <= end
