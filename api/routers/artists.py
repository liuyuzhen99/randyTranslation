from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from api.dependencies import get_artist_catalog_service
from application.services.artist_catalog_service import ArtistListFilters, CandidateListFilters

router = APIRouter(tags=["artists"])


def _build_pagination(page: int, page_size: int, total: int) -> dict:
    norm_page = max(page, 1)
    norm_size = min(max(page_size, 1), 100)
    total_pages = max((total + norm_size - 1) // norm_size, 1)
    return {"page": norm_page, "page_size": norm_size, "total": total, "total_pages": total_pages}


@router.get("/v1/artists")
async def list_artists(
    page: int = 1,
    page_size: int = 20,
    q: str = "",
    sync_status: str = "",
    sort: str = "candidate_count_desc",
    catalog_service=Depends(get_artist_catalog_service),
):
    from domain.time_utils import utc_now

    if catalog_service is None:
        raise HTTPException(status_code=503, detail="Artist catalog service is not enabled")
    items, total = catalog_service.list_artists(
        filters=ArtistListFilters(
            page=max(page, 1),
            page_size=min(max(page_size, 1), 100),
            query=q,
            sync_status=sync_status,
            sort=sort,
        ),
    )
    return {
        "items": items,
        "pagination": _build_pagination(page, page_size, total),
        "meta": {"generated_at": utc_now().isoformat(), "update_mode": "polling", "refresh_hint_seconds": 15},
    }


@router.get("/v1/artists/{artist_id}/candidates")
async def list_artist_candidates(
    artist_id: str,
    page: int = 1,
    page_size: int = 20,
    status: str = "",
    catalog_service=Depends(get_artist_catalog_service),
):
    from domain.time_utils import utc_now

    if catalog_service is None:
        raise HTTPException(status_code=503, detail="Artist catalog service is not enabled")
    if catalog_service.artist_repository.get(artist_id) is None:
        raise HTTPException(status_code=404, detail="Artist not found")
    items, total = catalog_service.list_candidates(
        artist_id=artist_id,
        filters=CandidateListFilters(
            page=max(page, 1),
            page_size=min(max(page_size, 1), 100),
            status=status,
        ),
    )
    return {
        "artist_id": artist_id,
        "items": items,
        "pagination": _build_pagination(page, page_size, total),
        "meta": {"generated_at": utc_now().isoformat(), "update_mode": "polling", "refresh_hint_seconds": 15},
    }


@router.post("/v1/artists/{artist_id}/resync")
async def resync_artist(
    artist_id: str,
    days: int = 14,
    catalog_service=Depends(get_artist_catalog_service),
):
    if catalog_service is None:
        raise HTTPException(status_code=503, detail="Artist catalog service is not enabled")
    try:
        return catalog_service.resync_artist(artist_id=artist_id, days=days, trigger="manual")
    except KeyError:
        raise HTTPException(status_code=404, detail="Artist not found")
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc))
