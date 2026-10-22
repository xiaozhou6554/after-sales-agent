"""受控下载 BAAI Cross-Encoder；运行时适配器只读取校验通过的本地副本。

用法：python -m infrastructure.retrieval.reranker_download --project-root <项目根目录>
下载约 1.11 GB 权重，需显式运行；导入此模块不会访问网络。
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

MODEL_ID = "BAAI/bge-reranker-base"
MODEL_REVISION = "2cfc18c9415c912f9d8155881c133215df768a70"
MODEL_LICENSE = "mit"
WEIGHTS_SHA256 = "ced967c45fd1902eb92716c9ceeca7c95a936770ea9db611f5a841b926e33fbd"
MODEL_FILES = (
    "README.md",
    "config.json",
    "model.safetensors",
    "sentencepiece.bpe.model",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
)
MANIFEST_NAME = "download-manifest.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _model_files(root: Path) -> dict[str, str]:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("重排模型目录无效")
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if relative.parts[0] == ".cache":
            continue  # huggingface_hub 下载元数据；不参与模型加载。
        if path.is_symlink() or (
            path.is_file() and relative.as_posix() not in (*MODEL_FILES, MANIFEST_NAME)
        ):
            raise ValueError("本地重排模型包含未登记文件或符号链接")
    checksums = {}
    for name in MODEL_FILES:
        file = root / name
        if file.is_symlink() or not file.is_file():
            raise ValueError(f"重排模型文件缺失: {name}")
        checksums[name] = _sha256(file)
    if checksums["model.safetensors"] != WEIGHTS_SHA256:
        raise ValueError("重排模型权重与官方固定提交的 SHA256 不符")
    card = (root / "README.md").read_text(encoding="utf-8")
    frontmatter = card.split("---", 2)
    if len(frontmatter) < 3 or "license: mit" not in frontmatter[1].lower():
        raise ValueError("重排模型许可声明不匹配")
    return checksums


def verify_model_directory(root: Path) -> Path:
    """离线校验来源、版本、许可和每个本地文件；拒绝 pickle 与多余文件。"""
    if root.is_symlink():
        raise ValueError("重排模型目录不能是符号链接")
    root = root.resolve()
    manifest_path = root / MANIFEST_NAME
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("重排模型缺少下载清单")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        manifest.get("source") != f"https://huggingface.co/{MODEL_ID}"
        or manifest.get("revision") != MODEL_REVISION
        or manifest.get("license") != MODEL_LICENSE
    ):
        raise ValueError("重排模型来源、版本或许可不匹配")
    checksums = _model_files(root)
    if manifest.get("files_sha256") != checksums:
        raise ValueError("重排模型文件哈希与清单不匹配")
    return root


def download_reranker(project_root: Path) -> Path:
    """仅在显式调用时联网，固定官方提交且只下载安全文件。"""
    project_root = project_root.resolve()
    target = project_root / "data" / "generated" / "models" / "bge-reranker-base"
    cache = project_root / "data" / "generated" / "hf-cache"
    if (target / MANIFEST_NAME).exists():
        return verify_model_directory(target)
    cache.mkdir(parents=True, exist_ok=True)
    from huggingface_hub import snapshot_download

    snapshot_download(
        repo_id=MODEL_ID,
        revision=MODEL_REVISION,
        local_dir=str(target),
        cache_dir=str(cache),
        allow_patterns=list(MODEL_FILES),
        ignore_patterns=["*.bin", "*.pt", "*.pth", "*.pkl", "*.pickle", "*.py"],
    )
    checksums = _model_files(target)
    manifest = {
        "source": f"https://huggingface.co/{MODEL_ID}",
        "revision": MODEL_REVISION,
        "license": MODEL_LICENSE,
        "files_sha256": checksums,
    }
    (target / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return verify_model_directory(target)


def main() -> None:
    parser = argparse.ArgumentParser(description="下载并校验固定版本的 BGE 中文重排模型")
    parser.add_argument("--project-root", type=Path, required=True)
    args = parser.parse_args()
    print(download_reranker(args.project_root))


if __name__ == "__main__":
    main()
