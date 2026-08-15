from __future__ import annotations

from collections import Counter

from backend.app.main import app


def test_openapi_operation_ids_are_unique() -> None:
    schema = app.openapi()
    operation_ids: list[str] = []
    for path_item in schema["paths"].values():
        for method, operation in path_item.items():
            if method.lower() in {"get", "post", "put", "patch", "delete", "options", "head"}:
                operation_id = operation.get("operationId")
                if operation_id:
                    operation_ids.append(operation_id)

    duplicates = {value: count for value, count in Counter(operation_ids).items() if count > 1}
    assert duplicates == {}
