"""Authenticated employee memory routes; business rules live in the business layer."""

from uuid import UUID

from fastapi import Request

from decagent.business.memory_control import BusinessMemoryControl as BusinessMemoryControl

from .models import (
    BusinessMemoryConfirmInput,
    BusinessMemoryCorrectionInput,
    BusinessMemoryRevocationInput,
    BusinessMemorySourceRevocationInput,
)


def install_business_memory_routes(app, database, mutate_employee, safe, scoped_database):
    control = BusinessMemoryControl(database)

    @app.get("/api/v2/business/memories")
    def memories(request: Request, task_id: UUID | None = None, limit: int = 50):
        return safe(control.history(request.state.employee, task_id=task_id, limit=limit))

    @app.get("/api/v2/business/memories/dependencies")
    def dependencies(request: Request, task_id: UUID):
        return safe(control.check_dependencies(request.state.employee, task_id))

    @app.post("/api/v2/business/memories/sources/revoke")
    def revoke_source(body: BusinessMemorySourceRevocationInput, request: Request):
        return mutate_employee(
            request,
            body,
            lambda conn, _: BusinessMemoryControl(scoped_database(database, conn)).revoke_source(
                request.state.employee, body
            ),
        )

    @app.get("/api/v2/business/memories/{memory_id}")
    def memory(memory_id: UUID, request: Request, task_id: UUID | None = None):
        return safe(control.get(request.state.employee, memory_id, task_id=task_id))

    @app.post("/api/v2/business/memories/{memory_id}/correct", status_code=201)
    def correct(memory_id: UUID, body: BusinessMemoryCorrectionInput, request: Request):
        return mutate_employee(
            request,
            body,
            lambda conn, _: BusinessMemoryControl(scoped_database(database, conn)).correct(
                request.state.employee, memory_id, body
            ),
            status=201,
        )

    @app.post("/api/v2/business/memories/{memory_id}/confirm")
    def confirm(memory_id: UUID, body: BusinessMemoryConfirmInput, request: Request):
        return mutate_employee(
            request,
            body,
            lambda conn, _: BusinessMemoryControl(scoped_database(database, conn)).confirm(
                request.state.employee, memory_id, body
            ),
        )

    @app.post("/api/v2/business/memories/{memory_id}/revoke")
    def revoke(memory_id: UUID, body: BusinessMemoryRevocationInput, request: Request):
        return mutate_employee(
            request,
            body,
            lambda conn, _: BusinessMemoryControl(scoped_database(database, conn)).revoke(
                request.state.employee, memory_id, body
            ),
        )
