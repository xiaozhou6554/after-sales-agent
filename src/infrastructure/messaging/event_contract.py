"""从唯一事件目录加载队列绑定，并用版本化 JSON Schema 校验载荷。"""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from application.common.events import EventEnvelope


class EventCatalog:
    def __init__(self, catalog_path: str | Path, schema_path: str | Path) -> None:
        catalog = json.loads(Path(catalog_path).read_text(encoding="utf-8"))
        schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        self.exchange = catalog["exchange"]
        self.dead_letter_queue = catalog["dead_letter_queue"]
        self.max_retries = int(catalog["max_retries"])
        self.entries = {item["event_type"]: item for item in catalog["events"]}
        if len(self.entries) != len(catalog["events"]):
            raise ValueError("事件目录存在重复 event_type")
        self._validator = Draft202012Validator(schema, format_checker=FormatChecker())

    def validate(self, event: EventEnvelope) -> dict:
        if event.event_type not in self.entries:
            raise ValueError(f"未登记事件类型: {event.event_type}")
        entry = self.entries[event.event_type]
        if event.producer != entry["producer"]:
            raise ValueError("事件生产者与目录不一致")
        errors = sorted(
            self._validator.iter_errors(event.to_dict()), key=lambda item: str(item.path)
        )
        if errors:
            raise ValueError(f"事件信封或载荷不符合契约: {errors[0].message}")
        return entry

    @property
    def queues(self) -> tuple[str, ...]:
        return tuple(sorted({item["queue"] for item in self.entries.values()}))

    @property
    def producers(self) -> tuple[str, ...]:
        return tuple(sorted({item["producer"] for item in self.entries.values()}))
