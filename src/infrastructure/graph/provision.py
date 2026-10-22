"""本地 Neo4j Enterprise 只读角色设置；需人工授权后才调用。"""

from __future__ import annotations

from neo4j import WRITE_ACCESS, exceptions

from infrastructure.persistence.neo4j.connection import create_neo4j_driver


def provision_development_reader(settings) -> int:
    if settings.app_env == "production":
        raise RuntimeError("生产凭据由部署平台提供，不通过开发脚本创建")
    if settings.neo4j_database != "after-sales-knowledge":
        raise RuntimeError("仅允许为固定开发知识数据库授予只读权限")
    if settings.neo4j_query_username != "knowledge_reader":
        raise RuntimeError("开发只读账号必须使用固定标识")
    if (
        len(settings.neo4j_query_password) < 12
        or settings.neo4j_query_password == settings.neo4j_password
    ):
        raise RuntimeError("请先注入至少12位、不同于维护账号的只读密码")
    admin = create_neo4j_driver(
        settings.neo4j_uri,
        settings.neo4j_username,
        settings.neo4j_password,
        settings.neo4j_connection_timeout_s,
    )
    try:
        commands = (
            "CREATE ROLE after_sales_knowledge_reader IF NOT EXISTS",
            "GRANT ACCESS ON DATABASE `after-sales-knowledge` TO after_sales_knowledge_reader",
            "GRANT MATCH {*} ON GRAPH `after-sales-knowledge` TO after_sales_knowledge_reader",
            "CREATE USER knowledge_reader IF NOT EXISTS SET PASSWORD $password CHANGE NOT REQUIRED",
            "GRANT ROLE after_sales_knowledge_reader TO knowledge_reader",
        )
        with admin.session(database="system") as session:
            for command in commands:
                session.run(command, password=settings.neo4j_query_password).consume()
    finally:
        admin.close()

    reader = create_neo4j_driver(
        settings.neo4j_uri,
        settings.neo4j_query_username,
        settings.neo4j_query_password,
        settings.neo4j_connection_timeout_s,
    )
    try:
        with reader.session(database=settings.neo4j_database) as session:
            count = session.run("MATCH (n) RETURN count(n) AS nodes").single()["nodes"]
        with reader.session(
            database=settings.neo4j_database, default_access_mode=WRITE_ACCESS
        ) as session:
            tx = session.begin_transaction()
            try:
                # 无标签写入探针，避免仅因“无权创建新标签”而误判为完全只读。
                tx.run("CREATE ()").consume()
            except (exceptions.AuthError, exceptions.Forbidden):
                pass
            else:
                raise AssertionError("只读账号意外拥有写入权限")
            finally:
                tx.rollback()
        return count
    finally:
        reader.close()
