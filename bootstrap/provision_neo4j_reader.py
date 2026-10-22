"""本地开发只读账号开通入口；需人工授权并注入密码后运行。"""

from bootstrap.settings import load_settings
from infrastructure.graph.provision import provision_development_reader


def main() -> None:
    count = provision_development_reader(load_settings())
    print(f"knowledge_reader verified read-only; visible_nodes={count}")


if __name__ == "__main__":
    main()
