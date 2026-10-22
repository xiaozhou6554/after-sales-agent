"""步骤17受控模型下载入口。"""

from __future__ import annotations

from pathlib import Path

from infrastructure.retrieval.model_download import download_model


def main() -> None:
    print(download_model(Path(__file__).resolve().parents[1]))


if __name__ == "__main__":
    main()
