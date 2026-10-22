from types import SimpleNamespace
from unittest.mock import MagicMock

from neo4j import exceptions

from infrastructure.graph import provision


def test_reader_provision_accepts_neo4j_forbidden_write_probe(monkeypatch) -> None:
    admin = MagicMock()
    reader = MagicMock()
    reader_session = reader.session.return_value.__enter__.return_value
    reader_session.run.return_value.single.return_value = {"nodes": 3}
    transaction = reader_session.begin_transaction.return_value
    transaction.run.side_effect = exceptions.Forbidden("write denied")

    def driver(_uri, username, _password, _timeout):
        return admin if username == "neo4j" else reader

    monkeypatch.setattr(provision, "create_neo4j_driver", driver)
    settings = SimpleNamespace(
        app_env="development",
        neo4j_database="after-sales-knowledge",
        neo4j_query_username="knowledge_reader",
        neo4j_query_password="separate-reader-secret",
        neo4j_password="admin-secret",
        neo4j_uri="bolt://localhost:7687",
        neo4j_username="neo4j",
        neo4j_connection_timeout_s=5,
    )

    assert provision.provision_development_reader(settings) == 3
    transaction.run.assert_called_once_with("CREATE ()")
    transaction.rollback.assert_called_once()
    admin.close.assert_called_once()
    reader.close.assert_called_once()
