"""Loopback-only Ark authorization, separate from native Doubao OAuth."""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from novelvideo.api.routes.doubao_work import local_only
from novelvideo.dual_reference_settings import public_status, save_config, verify_config

class PrivateConfigRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request):
            try:
                return await original(request)
            except RequestValidationError:
                # FastAPI's default error includes the submitted input, which may be a key.
                raise HTTPException(422, "配置格式无效，请检查密钥、服务地址和模型标识") from None
        return handler


router = APIRouter(
    prefix="/freezone/video/dual-reference",
    dependencies=[Depends(local_only)],
    tags=["双参考服务"],
    route_class=PrivateConfigRoute,
)


class DualReferenceConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    apiKey: str | None = Field(default=None, max_length=4096, repr=False)
    baseUrl: str | None = Field(default=None, max_length=500)
    model: str | None = Field(default=None, max_length=200)


@router.get("/config")
def get_config():
    return {"ok": True, "data": public_status()}


@router.put("/config")
def put_config(body: DualReferenceConfigRequest):
    try:
        data = save_config(api_key=body.apiKey, base_url=body.baseUrl, model=body.model)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return {"ok": True, "data": data}


@router.post("/verify")
async def verify():
    return {"ok": True, "data": await verify_config()}
