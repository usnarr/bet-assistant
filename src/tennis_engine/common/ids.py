"""Opaque random identifiers and deterministic fixture identifiers."""

from uuid import UUID, uuid4, uuid5

ENGINE_NAMESPACE = UUID("b5550eb0-e9a7-4e8e-9511-e8a0a99a56d1")


def new_id() -> UUID:
    return uuid4()


def stable_id(namespace: str, value: str) -> UUID:
    if not namespace.strip() or not value.strip():
        raise ValueError("Deterministic IDs require a namespace and value")
    return uuid5(ENGINE_NAMESPACE, f"{namespace}:{value}")
