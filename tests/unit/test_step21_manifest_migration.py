"""Step 21 manifest snapshot migration contract."""

from bootstrap.migrate import discover_versions
from migrations.versions.v0011_knowledge_manifest import VERSION, statements


def test_manifest_migration_is_discovered_after_release_migration() -> None:
    versions = [module.VERSION for module in discover_versions()]
    assert VERSION == "0011"
    assert versions.index("0009") < versions.index(VERSION)


def test_manifest_snapshot_has_release_scoped_key_and_complete_json() -> None:
    ddl = statements()
    assert len(ddl) == 1
    statement = ddl[0]
    assert statement.startswith("CREATE TABLE IF NOT EXISTS knowledge_manifest (")
    assert "tenant_id VARCHAR(64) NOT NULL" in statement
    assert "knowledge_release_id VARCHAR(64) NOT NULL" in statement
    assert "manifest_json LONGTEXT NOT NULL" in statement
    assert "manifest_digest CHAR(64) NOT NULL" in statement
    assert "PRIMARY KEY (tenant_id, knowledge_release_id)" in statement
    assert "FOREIGN KEY (tenant_id, knowledge_release_id)" in statement
    assert "REFERENCES knowledge_release (tenant_id, knowledge_release_id)" in statement
    assert "ENGINE=InnoDB" in statement


def test_manifest_ddl_is_replay_safe_and_contains_no_mutation() -> None:
    assert statements() == statements()
    assert all(";" not in statement for statement in statements())
    assert all(statement.startswith("CREATE TABLE IF NOT EXISTS ") for statement in statements())
