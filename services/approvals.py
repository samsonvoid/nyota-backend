from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_APPROVAL_TTL_SECONDS = 30


@dataclass
class ApprovalRequest:
    approval_id: str
    tool: str
    arguments: dict[str, Any]
    created_at: float
    expires_at: float
    status: str = "pending"
    result: dict[str, Any] | None = None
    decided_at: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "tool": self.tool,
            "arguments": self.arguments,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "status": self.status,
            "result": self.result,
        }


class ApprovalStore:
    def __init__(self, ttl_seconds: int = DEFAULT_APPROVAL_TTL_SECONDS):
        self.ttl_seconds = ttl_seconds
        self._requests: dict[str, ApprovalRequest] = {}
        self._lock = threading.Lock()

    def create(self, tool: str, arguments: dict[str, Any]) -> ApprovalRequest:
        now = time.time()
        request = ApprovalRequest(
            approval_id=uuid.uuid4().hex,
            tool=tool,
            arguments=arguments,
            created_at=now,
            expires_at=now + self.ttl_seconds,
        )
        with self._lock:
            self._expire_locked(now)
            self._requests[request.approval_id] = request
        self._audit(request, "pending")
        return request

    def get(self, approval_id: str) -> ApprovalRequest | None:
        with self._lock:
            self._expire_locked(time.time())
            return self._requests.get(approval_id)

    def decide(self, approval_id: str, decision: str) -> ApprovalRequest | None:
        with self._lock:
            self._expire_locked(time.time())
            request = self._requests.get(approval_id)
            if not request or request.status != "pending":
                return request
            request.status = decision
            request.decided_at = time.time()
        self._audit(request, decision)
        return request

    def complete(self, request: ApprovalRequest, result: dict[str, Any]) -> None:
        with self._lock:
            request.status = "completed" if result.get("success") else "failed"
            request.result = result
            request.decided_at = time.time()
        self._audit(request, request.status)

    def _expire_locked(self, now: float) -> None:
        for request in self._requests.values():
            if request.status == "pending" and request.expires_at <= now:
                request.status = "expired"
                request.decided_at = now
                self._audit(request, "expired")

    def _audit(self, request: ApprovalRequest, event: str) -> None:
        audit_path = Path(os.getenv("NYOTA_APPROVAL_AUDIT_LOG", "data/approval_audit.jsonl"))
        try:
            audit_path.parent.mkdir(parents=True, exist_ok=True)
            record = {
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "event": event,
                "approval_id": request.approval_id,
                "tool": request.tool,
                "arguments": request.arguments,
            }
            with audit_path.open("a", encoding="utf-8") as audit_file:
                audit_file.write(json.dumps(record, ensure_ascii=True) + "\n")
        except OSError as error:
            print(f"[APPROVAL]: Could not write audit log: {error}")


approval_store = ApprovalStore()
