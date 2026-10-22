"""加载与冻结开发标签、语料、模型修订绑定的 RAG 候选参数。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from application.knowledge.rag import CalibrationCurve, RagConfig
from infrastructure.retrieval.reranker_download import MODEL_ID, MODEL_REVISION

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "data/config/knowledge-rag-development-v1.json"
LABELS = ROOT / "data/scenarios/v2/knowledge_labels.jsonl"
CORPUS = ROOT / "data/knowledge/v2/corpus.json"


def _curve(value: dict) -> CalibrationCurve:
    return CalibrationCurve(
        version=value["version"],
        points=tuple((float(raw), float(relevance)) for raw, relevance in value["points"]),
    )


def load_development_rag_config() -> RagConfig:
    payload = json.loads(CONFIG.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "1.0" or payload.get("selection_split") != "development":
        raise ValueError("RAG 参数配置版本或选择数据分区无效")
    if hashlib.sha256(LABELS.read_bytes()).hexdigest() != payload.get(
        "labels_sha256"
    ) or hashlib.sha256(CORPUS.read_bytes()).hexdigest() != payload.get("corpus_sha256"):
        raise ValueError("冻结开发标签或语料与 RAG 参数配置不一致")
    if (
        payload.get("reranker_model_id") != MODEL_ID
        or payload.get("reranker_revision") != MODEL_REVISION
    ):
        raise ValueError("RAG 参数与重排模型修订不一致")
    return RagConfig(
        version=payload["config_version"],
        vector_calibration=_curve(payload["vector_calibration"]),
        bm25_calibration=_curve(payload["bm25_calibration"]),
        rerank_calibration=_curve(payload["rerank_calibration"]),
        vector_weight=float(payload["vector_weight"]),
        bm25_weight=float(payload["bm25_weight"]),
        vector_threshold=float(payload["vector_threshold"]),
        bm25_threshold=float(payload["bm25_threshold"]),
        fused_threshold=float(payload["fused_threshold"]),
        rerank_threshold=float(payload["rerank_threshold"]),
        vector_top_k=int(payload["vector_top_k"]),
        bm25_top_k=int(payload["bm25_top_k"]),
        result_top_k=int(payload["result_top_k"]),
    )
