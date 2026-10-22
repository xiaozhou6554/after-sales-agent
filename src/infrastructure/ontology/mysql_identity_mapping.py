"""时态商品映射读取与解析：源代码→规范化商品，按租户隔离（TC-95/96）。"""

from __future__ import annotations

from datetime import date

from sqlalchemy import text
from sqlalchemy.engine import Engine

from domain.ontology.mapping_rules import (
    MappingRecord,
    Resolution,
    resolve_mapping,
    validate_mapping_set,
)


class MySqlIdentityMapping:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def load_records(self, tenant_id: str) -> list[MappingRecord]:
        with self._engine.connect() as connection:
            rows = connection.execute(text(
                "SELECT tenant_id,source_system,source_id,version,canonical_product_id,"
                " valid_from,valid_to,status FROM mock_pim_product_mapping"
                " WHERE tenant_id=:t"
            ), {"t": tenant_id}).mappings().all()
        return [
            MappingRecord(
                tenant_id=row["tenant_id"],
                source_system=row["source_system"],
                source_id=row["source_id"],
                version=int(row["version"]),
                canonical_product_id=row["canonical_product_id"],
                valid_from=row["valid_from"],
                valid_to=row["valid_to"],
                status=row["status"],
            )
            for row in rows
        ]

    def tenant_products(self, tenant_id: str) -> set[str]:
        with self._engine.connect() as connection:
            rows = connection.execute(text(
                "SELECT canonical_product_id FROM mock_pim_product WHERE tenant_id=:t"
            ), {"t": tenant_id}).all()
        return {row[0] for row in rows}

    def validate_tenant(self, tenant_id: str) -> list:
        return validate_mapping_set(
            self.load_records(tenant_id), {tenant_id: self.tenant_products(tenant_id)}
        )

    def resolve(
        self, tenant_id: str, source_system: str, source_id: str, on_date: date
    ) -> Resolution:
        rows = [
            row for row in self.load_records(tenant_id)
            if row.source_system == source_system and row.source_id == source_id
        ]
        return resolve_mapping(rows, on_date)

    def resolver(self, tenant_id: str):
        """预载一次映射后返回内存解析函数；投影批量解析避免逐行查库。"""
        records = self.load_records(tenant_id)
        by_source: dict[tuple[str, str], list[MappingRecord]] = {}
        for row in records:
            by_source.setdefault((row.source_system, row.source_id), []).append(row)

        def resolve(
            source_system: str, source_id: str, on_date: date
        ) -> Resolution:
            return resolve_mapping(
                by_source.get((source_system, source_id), []), on_date
            )

        return resolve
