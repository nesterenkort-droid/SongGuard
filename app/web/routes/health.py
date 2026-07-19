"""Machine-readable health endpoint.

`/healthz` is what Docker's healthcheck, the external dead-man switch and any
uptime monitor hit. Returns 200 while critical components are up, 503 when the
system is DOWN so orchestrators can react.
"""

from fastapi import APIRouter, Response

from app.health import DOWN, gather_health

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz(response: Response) -> dict:
    overall, components = await gather_health()
    if overall == DOWN:
        response.status_code = 503
    return {
        "status": overall,
        "components": [c.as_dict() for c in components],
    }
