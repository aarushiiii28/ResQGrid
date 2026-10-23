from collections.abc import Callable
from typing import Generic, TypeVar

from pydantic import BaseModel

from app.db.pg import Store

T = TypeVar("T", bound=BaseModel)


class Repo(Generic[T]):
    def __init__(self, store: Store, kind: str, model: type[T]):
        self.store, self.kind, self.model = store, kind, model

    async def get(self, id_: str) -> T | None:
        d = await self.store.get(self.kind, id_)
        return self.model.model_validate(d) if d else None

    async def put(self, obj: T) -> T:
        await self.store.put(self.kind, obj.id, obj.model_dump(mode="json"))  # type: ignore[attr-defined]
        return obj

    async def list(self) -> list[T]:
        return [self.model.model_validate(d) for d in await self.store.list(self.kind)]

    async def modify(
        self, id_: str, fn: Callable[[T], T | None], *, claim_key: str | None = None
    ) -> tuple[T, str]:
        def raw(d: dict) -> dict | None:
            out = fn(self.model.model_validate(d))
            return None if out is None else out.model_dump(mode="json")

        data, outcome = await self.store.modify(self.kind, id_, raw, claim_key=claim_key)
        return self.model.model_validate(data), outcome
