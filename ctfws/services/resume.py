"""Manual post-restart reconciliation and resume plans."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from typing import Any

from ctfws.models.context import ContextStatus
from ctfws.models.forward import ForwardStatus
from ctfws.services.contexts import NetworkContextService
from ctfws.services.processes import ForwardProcessService
from ctfws.services.workspace import WorkspaceService


@dataclass(frozen=True, slots=True)
class ResumeItem:
    """One resource which needs explicit operator action after restart."""

    resource_type: str
    resource_id: int
    name: str
    action: str
    dependency_ids: tuple[int, ...] = ()
    reason: str = ""


class ResumeService:
    """Rebuild visual organization without silently reactivating resources."""

    def __init__(
        self,
        workspace: WorkspaceService,
        forward_service: Any | None = None,
        context_service: Any | None = None,
    ) -> None:
        self.workspace = workspace
        self.forward_service = forward_service
        self.context_service = context_service

    def plan(self) -> list[ResumeItem]:
        items: list[ResumeItem] = []
        for forward in self.workspace.forwards.list():
            if forward.status in {
                ForwardStatus.ACTIVE,
                ForwardStatus.STARTING,
                ForwardStatus.DEGRADED,
            }:
                items.append(
                    ResumeItem(
                        "forward",
                        forward.id,
                        forward.name,
                        "revalidate-or-start",
                        forward.dependency_ids,
                        "O túnel não será iniciado automaticamente após o restart.",
                    )
                )
        for context in self.workspace.contexts.list():
            if context.status in {
                ContextStatus.ACTIVE,
                ContextStatus.STARTING,
                ContextStatus.DEGRADED,
            }:
                items.append(
                    ResumeItem(
                        "context",
                        context.id,
                        context.name,
                        "revalidate-or-start",
                        (),
                        "O contexto exige confirmação explícita do operador.",
                    )
                )
        return items

    def apply(
        self,
        resource_ids: tuple[int, ...] | None = None,
        resource_refs: tuple[str, ...] | None = None,
    ) -> list[dict[str, object]]:
        """Apply only the selected resume plan, in dependency order."""

        plan = self.plan()
        selected = self._selection(plan, resource_ids, resource_refs)
        results: list[dict[str, object]] = []
        for item in self._ordered(plan):
            if selected is not None and self._resource_ref(item) not in selected:
                continue
            if item.resource_type == "forward":
                forward = ForwardProcessService(self.workspace).check(item.resource_id)
                if forward.status in {ForwardStatus.STOPPED, ForwardStatus.ERROR}:
                    forward = ForwardProcessService(self.workspace).start(item.resource_id)
                results.append(
                    {
                        "resource_type": item.resource_type,
                        "resource": forward.model_dump(mode="json"),
                    }
                )
            else:
                context = NetworkContextService(self.workspace).start(item.resource_id)
                results.append(
                    {
                        "resource_type": item.resource_type,
                        "resource": context.model_dump(mode="json"),
                    }
                )
        return results

    async def apply_async(
        self,
        resource_ids: tuple[int, ...] | None = None,
        resource_refs: tuple[str, ...] | None = None,
    ) -> list[dict[str, object]]:
        """Apply a selected plan using the live web motor's owned resources."""

        plan = self.plan()
        selected = self._selection(plan, resource_ids, resource_refs)
        results: list[dict[str, object]] = []
        for item in self._ordered(plan):
            if selected is not None and self._resource_ref(item) not in selected:
                continue
            if item.resource_type == "forward":
                service = self.forward_service or ForwardProcessService(self.workspace)
                forward = (
                    await service.check_async(item.resource_id)
                    if self.forward_service
                    else await asyncio.to_thread(service.check, item.resource_id)
                )
                if forward.status in {
                    ForwardStatus.STOPPED,
                    ForwardStatus.ERROR,
                    ForwardStatus.DEGRADED,
                }:
                    forward = (
                        await service.start_async(item.resource_id)
                        if self.forward_service
                        else await asyncio.to_thread(service.start, item.resource_id)
                    )
                results.append(
                    {
                        "resource_type": item.resource_type,
                        "resource": forward.model_dump(mode="json"),
                    }
                )
            else:
                service = self.context_service or NetworkContextService(self.workspace)
                context = (
                    await service.start_async(item.resource_id)
                    if self.context_service
                    else await asyncio.to_thread(service.start, item.resource_id)
                )
                results.append(
                    {
                        "resource_type": item.resource_type,
                        "resource": context.model_dump(mode="json"),
                    }
                )
        return results

    def _selection(
        self,
        plan: list[ResumeItem],
        resource_ids: tuple[int, ...] | None,
        resource_refs: tuple[str, ...] | None,
    ) -> set[str] | None:
        selected: set[str] | None = None
        if resource_ids or resource_refs:
            selected = set(resource_refs or ())
            selected.update(
                self._resource_ref(item)
                for item in plan
                if resource_ids and item.resource_id in resource_ids
            )
            valid_refs = {self._resource_ref(item) for item in plan}
            unknown = selected - valid_refs
            if unknown:
                raise ValueError(
                    "Recursos de retomada não encontrados: " + ", ".join(sorted(unknown))
                )
            changed = True
            while changed:
                changed = False
                for item in plan:
                    if self._resource_ref(item) in selected:
                        for dependency in item.dependency_ids:
                            dependency_ref = f"forward:{dependency}"
                            if dependency_ref in valid_refs and dependency_ref not in selected:
                                selected.add(dependency_ref)
                                changed = True
        return selected

    @staticmethod
    def _ordered(items: list[ResumeItem]) -> list[ResumeItem]:
        pending = {(item.resource_type, item.resource_id): item for item in items}
        ordered: list[ResumeItem] = []
        while pending:
            ready = [
                item
                for item in pending.values()
                if all(("forward", dependency) not in pending for dependency in item.dependency_ids)
            ]
            if not ready:
                raise ValueError("Dependências de retomada contêm um ciclo.")
            for item in sorted(ready, key=lambda value: (value.resource_type, value.resource_id)):
                ordered.append(item)
                pending.pop((item.resource_type, item.resource_id), None)
        return ordered

    def as_dicts(self) -> list[dict[str, object]]:
        return [{**asdict(item), "resource_ref": self._resource_ref(item)} for item in self.plan()]

    @staticmethod
    def _resource_ref(item: ResumeItem) -> str:
        return f"{item.resource_type}:{item.resource_id}"
