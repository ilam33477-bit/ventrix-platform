from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from ..schemas import (
    AIProfileRead,
    AIProfileUpdate,
    BotCreate,
    BotRead,
    TenantCreate,
    TenantRead,
    TenantUpdate,
)
from ..services.foundation import BotAlreadyExistsError, FoundationService, TenantNotFoundError
from ..services.telegram import BotTokenVerificationError
from .dependencies import get_foundation_service, require_owner_api_token

router = APIRouter(
    prefix="/api/v1/owner",
    dependencies=[Depends(require_owner_api_token)],
    tags=["platform-owner"],
)
Service = Annotated[FoundationService, Depends(get_foundation_service)]


def not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="tenant not found")


@router.get("/tenants", response_model=list[TenantRead])
async def list_tenants(service: Service) -> list[object]:
    return await service.list_tenants()


@router.post("/tenants", response_model=TenantRead, status_code=status.HTTP_201_CREATED)
async def create_tenant(payload: TenantCreate, service: Service) -> object:
    return await service.create_tenant(payload)


@router.get("/tenants/{tenant_id}", response_model=TenantRead)
async def get_tenant(tenant_id: UUID, service: Service) -> object:
    try:
        return await service.get_tenant(tenant_id)
    except TenantNotFoundError:
        raise not_found() from None


@router.patch("/tenants/{tenant_id}", response_model=TenantRead)
async def update_tenant(tenant_id: UUID, payload: TenantUpdate, service: Service) -> object:
    try:
        return await service.update_tenant(tenant_id, payload)
    except TenantNotFoundError:
        raise not_found() from None


@router.get("/tenants/{tenant_id}/ai-profile", response_model=AIProfileRead)
async def get_ai_profile(tenant_id: UUID, service: Service) -> object:
    try:
        tenant = await service.get_tenant(tenant_id)
        return tenant.ai_profile
    except TenantNotFoundError:
        raise not_found() from None


@router.patch("/tenants/{tenant_id}/ai-profile", response_model=AIProfileRead)
async def update_ai_profile(tenant_id: UUID, payload: AIProfileUpdate, service: Service) -> object:
    try:
        return await service.update_ai_profile(tenant_id, payload)
    except TenantNotFoundError:
        raise not_found() from None


@router.post(
    "/tenants/{tenant_id}/bots", response_model=BotRead, status_code=status.HTTP_201_CREATED
)
async def create_bot(tenant_id: UUID, payload: BotCreate, service: Service) -> object:
    try:
        return await service.create_bot(tenant_id, payload)
    except TenantNotFoundError:
        raise not_found() from None
    except BotTokenVerificationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from None
    except BotAlreadyExistsError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None


@router.get("/tenants/{tenant_id}/bots", response_model=list[BotRead])
async def list_bots(tenant_id: UUID, service: Service) -> list[object]:
    try:
        return await service.list_bots(tenant_id)
    except TenantNotFoundError:
        raise not_found() from None
