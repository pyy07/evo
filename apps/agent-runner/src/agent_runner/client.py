from __future__ import annotations

from typing import Any

import httpx


class SystemClient:
    def __init__(self, base_url: str, actor: str = "agent-runner") -> None:
        self.base_url = base_url.rstrip("/")
        self.actor = actor
        self.agent_run_id: int | None = None

    def begin_run(self, run_id: int) -> None:
        self.agent_run_id = int(run_id)

    def end_run(self) -> None:
        self.agent_run_id = None

    def invoke(self, capability_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = dict(payload or {})
        if self.agent_run_id is not None and "agent_run_id" not in body:
            body["agent_run_id"] = self.agent_run_id
        with httpx.Client(base_url=self.base_url, timeout=60.0) as client:
            r = client.post(
                f"/capabilities/{capability_id}/invoke",
                json={"input": body},
                headers={"X-Actor": self.actor},
            )
            if r.status_code >= 400:
                detail = r.text
                try:
                    detail = r.json().get("detail", detail)
                except Exception:  # noqa: BLE001
                    pass
                raise httpx.HTTPStatusError(
                    f"{capability_id} failed: {r.status_code} {detail}",
                    request=r.request,
                    response=r,
                )
            return r.json()["result"]

    def list_capabilities(self) -> list[dict[str, Any]]:
        return self.invoke("list_capabilities")["capabilities"]