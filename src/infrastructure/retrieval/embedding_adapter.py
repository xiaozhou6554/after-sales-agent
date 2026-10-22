"""受控本地 BGE Embedding；模型缺失时明确报故障，不自动联网下载。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from application.knowledge.retrieval.ports import RetrievalUnavailable


class LocalSentenceEmbedding:
    def __init__(
        self,
        model_id: str,
        local_dir: Path | None = None,
        expected_revision: str | None = None,
    ) -> None:
        self.model_id = model_id
        self._local_dir = local_dir
        self._expected_revision = expected_revision
        self._model = None

    def _verified_model_path(self) -> str:
        if self._local_dir is None:
            return self.model_id
        root = self._local_dir.resolve()
        manifest = json.loads((root / "download-manifest.json").read_text(encoding="utf-8"))
        if manifest.get("source") != f"https://huggingface.co/{self.model_id}":
            raise ValueError("本地模型来源不匹配")
        if self._expected_revision and manifest.get("revision") != self._expected_revision:
            raise ValueError("本地模型提交版本不匹配")
        checksums = manifest.get("files_sha256") or {}
        if not {"model.safetensors", "config.json", "modules.json"} <= set(checksums):
            raise ValueError("本地模型清单不完整")
        if (root / "pytorch_model.bin").exists():
            raise ValueError("本地模型含未授权的pickle权重")
        for name, expected in checksums.items():
            path = (root / name).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise ValueError("本地模型文件缺失或越界")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != expected:
                raise ValueError(f"本地模型文件哈希不匹配: {name}")
        return str(root)

    def encode(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            if self._model is None:
                from sentence_transformers import SentenceTransformer

                self._model = SentenceTransformer(
                    self._verified_model_path(),
                    local_files_only=True,
                    trust_remote_code=False,
                    device="cpu",
                )
            vectors = self._model.encode(
                texts,
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
            )
            return vectors.astype("float32").tolist()
        except Exception as exc:
            raise RetrievalUnavailable("本地 Embedding 模型不可用或推理失败") from exc
