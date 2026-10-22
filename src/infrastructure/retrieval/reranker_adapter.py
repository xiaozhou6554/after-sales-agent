"""离线本地 Cross-Encoder；模型缺失或不可信时明确失败。"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Sequence

from application.knowledge.rag.rerank import RerankUnavailable
from infrastructure.retrieval.reranker_download import (
    MODEL_ID,
    MODEL_REVISION,
    verify_model_directory,
)


class LocalCrossEncoderReranker:
    model_id = MODEL_ID
    model_revision = MODEL_REVISION

    def __init__(self, local_dir: Path, *, max_length: int = 512, batch_size: int = 8) -> None:
        if max_length <= 0 or batch_size <= 0:
            raise ValueError("重排长度与批次必须大于零")
        self._local_dir = local_dir
        self._max_length = max_length
        self._batch_size = batch_size
        self._model = None

    def score(self, query: str, passages: Sequence[str]) -> tuple[float, ...]:
        if not passages:
            return ()
        if not query.strip() or any(not passage.strip() for passage in passages):
            raise ValueError("重排查询和候选文本不能为空")
        try:
            if self._model is None:
                path = verify_model_directory(self._local_dir)
                from sentence_transformers import CrossEncoder

                self._model = CrossEncoder(
                    str(path),
                    device="cpu",
                    max_length=self._max_length,
                    local_files_only=True,
                    trust_remote_code=False,
                    model_kwargs={"use_safetensors": True},
                )
            raw = self._model.predict(
                [(query, passage) for passage in passages],
                batch_size=self._batch_size,
                show_progress_bar=False,
                convert_to_numpy=True,
                activation_fn=lambda logits: logits,
            )
            scores = tuple(float(value) for value in raw)
            if len(scores) != len(passages) or any(not math.isfinite(score) for score in scores):
                raise ValueError("Cross-Encoder 返回无效分数")
            return scores
        except Exception as exc:
            raise RerankUnavailable("本地重排模型不可用或推理失败") from exc
