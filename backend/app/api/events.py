"""Lambda Web Adapter pass-through for non-HTTP events (the scheduled cleanup invoke).

Mounted only when settings.cleanup_mode is true, i.e. on the private cleanup function.
"""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter

from app.core.settings import get_settings
from app.db.engine import get_sessionmaker
from app.ops.cleanup import purge_expired

router = APIRouter()


@router.post("/events")
async def run_cleanup() -> dict[str, int]:
    retention = timedelta(hours=get_settings().retention_hours)
    return await purge_expired(get_sessionmaker(), now=datetime.now(UTC), retention=retention)
