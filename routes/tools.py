import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from middleware.rate_limit import limiter
from middleware.security import validate_api_key
from services.approvals import approval_store
from services.executor import available_tools, execute_tool
from services.memory import add_workspace, list_workspaces, remove_workspace


router = APIRouter(prefix="/api/tools", tags=["tools"])


class ToolExecutionRequest(BaseModel):
    tool: str = Field(..., min_length=1, max_length=80)
    arguments: dict[str, Any] = Field(default_factory=dict)


class WorkspaceRequest(BaseModel):
    path: str = Field(..., min_length=3, max_length=500)
    name: str | None = Field(default=None, max_length=120)
    purpose: str | None = Field(default=None, max_length=240)


@router.get("/workspaces")
@limiter.limit("30/minute")
async def get_workspaces(request: Request, _=Depends(validate_api_key)):
    return {"workspaces": list_workspaces()}


@router.post("/workspaces")
@limiter.limit("20/minute")
async def create_workspace(
    request: Request,
    body: WorkspaceRequest,
    _=Depends(validate_api_key),
):
    try:
        workspace = add_workspace(body.path, body.name, body.purpose)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"workspace": workspace, "workspaces": list_workspaces()}


@router.delete("/workspaces")
@limiter.limit("20/minute")
async def delete_workspace(
    request: Request,
    body: WorkspaceRequest,
    _=Depends(validate_api_key),
):
    try:
        remove_workspace(body.path)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"workspaces": list_workspaces()}


@router.get("")
@limiter.limit("30/minute")
async def list_tools(request: Request, _=Depends(validate_api_key)):
    return {"tools": available_tools()}


@router.post("/execute")
@limiter.limit("20/minute")
async def run_tool(
    request: Request,
    body: ToolExecutionRequest,
    _=Depends(validate_api_key),
):
    tool_definition = next(
        (tool for tool in available_tools() if tool["name"] == body.tool),
        None,
    )
    if tool_definition is None:
        raise HTTPException(status_code=404, detail=f"Unknown or unsupported tool: {body.tool}")
    if tool_definition["requires_approval"]:
        approval = approval_store.create(body.tool, body.arguments)
        return {
            "status": "pending_approval",
            "approval": approval.as_dict(),
        }

    result = await asyncio.to_thread(execute_tool, body.tool, body.arguments)
    return result


@router.get("/approvals/{approval_id}")
@limiter.limit("60/minute")
async def get_approval(approval_id: str, request: Request, _=Depends(validate_api_key)):
    approval = approval_store.get(approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail="Approval request not found")
    return approval.as_dict()


@router.post("/approvals/{approval_id}/approve")
@limiter.limit("20/minute")
async def approve_tool(approval_id: str, request: Request, _=Depends(validate_api_key)):
    approval = approval_store.decide(approval_id, "approved")
    if approval is None:
        raise HTTPException(status_code=404, detail="Approval request not found")
    if approval.status != "approved":
        return approval.as_dict()

    result = await asyncio.to_thread(
        execute_tool,
        approval.tool,
        approval.arguments,
        True,
    )
    approval_store.complete(approval, result)
    
    # Return approval with tool result included
    response = approval.as_dict()
    response["tool_result"] = result
    return response


@router.post("/approvals/{approval_id}/reject")
@limiter.limit("30/minute")
async def reject_tool(approval_id: str, request: Request, _=Depends(validate_api_key)):
    approval = approval_store.decide(approval_id, "rejected")
    if approval is None:
        raise HTTPException(status_code=404, detail="Approval request not found")
    return approval.as_dict()
