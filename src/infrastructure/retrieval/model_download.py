"""从官方仓库下载固定提交的安全权重并生成本地哈希清单。"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

REPO_ID = "BAAI/bge-small-zh-v1.5"
REVISION = "7999e1d3359715c523056ef9478215996d62a620"
FILES = (
    "1_Pooling/config.json",
    "README.md",
    "config.json",
    "config_sentence_transformers.json",
    "model.safetensors",
    "modules.json",
    "sentence_bert_config.json",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "vocab.txt",
)


def download_model(project_root: Path) -> str:
    target = project_root / "data" / "generated" / "models" / "bge-small-zh-v1.5"
    cache = project_root / "data" / "generated" / "hf-cache"
    cache.mkdir(parents=True, exist_ok=True)
    os.environ["HF_HOME"] = str(cache)
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    from huggingface_hub import snapshot_download

    snapshot_download(
        repo_id=REPO_ID,
        revision=REVISION,
        local_dir=target,
        allow_patterns=list(FILES),
    )
    checksums = {}
    for name in FILES:
        path = target / name
        if not path.is_file():
            raise RuntimeError(f"模型文件缺失: {name}")
        checksums[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    if (target / "pytorch_model.bin").exists():
        raise RuntimeError("不应下载 pickle 格式的权重")
    metadata = {
        "source": f"https://huggingface.co/{REPO_ID}",
        "revision": REVISION,
        "license": "mit",
        "files_sha256": checksums,
    }
    (target / "download-manifest.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return f"已核对受控模型: {REPO_ID}@{REVISION}; {len(FILES)} 个文件"
