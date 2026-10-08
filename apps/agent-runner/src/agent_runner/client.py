from __future__ import annotations

from typing import Any

import httpx


class SystemClient:
    def __init__(self, base_url: str, actor: str = "agent-runner") -> None:
        self.base_url = base_url.rstrip("/")
        self.actor = actor

    def invoke(self, capability_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        with httpx.Client(base_url=self.base_url, timeout=60.0) as client:
            r = client.post(
                f"/capabilities/{capability_id}/invoke",
                json={"input": payload or {}},
                headers={"X-Actor": self.actor},
            )
            r.raise_for_status()
            return r.json()["result"]

    def list_capabilities(self) -> list[dict[str, Any]]:
        return self.invoke("list_capabilities")["capabilities"]