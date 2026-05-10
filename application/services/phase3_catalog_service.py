from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from domain.entities import Artist, ArtistSyncRun, VideoCandidate
from domain.enums import CandidateStatus, SyncStatus
from domain.repositories import ArtistRepository, ArtistSyncRunRepository, CandidateRepository
from domain.time_utils import utc_now


@dataclass(frozen=True)
class CandidateDiscoveryPayload:
    video_id: str
    title: str
    source_url: str
    published_at: datetime | None = None


@dataclass(frozen=True)
class Phase3Providers:
    followed_artists_lookup: Callable[[], list[Artist]]
    channel_lookup: Callable[[Artist], str | None]
    candidate_lookup: Callable[[Artist, int], list[CandidateDiscoveryPayload]]


@dataclass(frozen=True)
class ArtistListFilters:
    page: int = 1
    page_size: int = 20
    query: str = ""
    sync_status: str = ""
    sort: str = "candidate_count_desc"


@dataclass(frozen=True)
class CandidateListFilters:
    page: int = 1
    page_size: int = 20
    status: str = ""


class ArtistSyncService:
    def __init__(
        self,
        artist_repository: ArtistRepository,
        artist_sync_run_repository: ArtistSyncRunRepository,
    ) -> None:
        self.artist_repository = artist_repository
        self.artist_sync_run_repository = artist_sync_run_repository

    def sync_followed_artists(
        self,
        provider: Callable[[], list[Artist]],
        trigger: str = "manual",
    ) -> dict:
        imported_artists = provider()
        existing_artists = {artist.spotify_id: artist for artist in self.artist_repository.list_all()}
        imported_ids = {artist.spotify_id for artist in imported_artists}
        synced_count = 0
        created_count = 0
        updated_count = 0
        completed_at = utc_now()
        for artist in imported_artists:
            existing = self.artist_repository.get(artist.spotify_id)
            sync_started_at = existing.last_sync_started_at if existing else completed_at
            normalized_artist = Artist(
                spotify_id=artist.spotify_id,
                name=artist.name,
                yt_channel_id=(existing.yt_channel_id if existing else None) or artist.yt_channel_id,
                status=artist.status,
                sync_status=SyncStatus.COMPLETED,
                last_sync_started_at=sync_started_at,
                last_sync_completed_at=completed_at,
                last_sync_error=None,
                last_channel_resolved_at=existing.last_channel_resolved_at if existing else None,
                last_discovery_at=existing.last_discovery_at if existing else None,
            )
            self.artist_repository.upsert(normalized_artist)
            self.artist_sync_run_repository.create(
                ArtistSyncRun(
                    run_id=str(uuid.uuid4()),
                    spotify_id=artist.spotify_id,
                    source_kind="spotify_following",
                    status=SyncStatus.COMPLETED,
                    started_at=completed_at,
                    completed_at=completed_at,
                    failure_reason=None,
                    retry_count=0,
                    discovered_count=1,
                    trigger=trigger,
                )
            )
            synced_count += 1
            if existing is None:
                created_count += 1
            else:
                updated_count += 1

        for artist_id, existing in existing_artists.items():
            if artist_id in imported_ids or existing.status != "active":
                continue
            self.artist_repository.upsert(
                Artist(
                    spotify_id=existing.spotify_id,
                    name=existing.name,
                    yt_channel_id=existing.yt_channel_id,
                    status="inactive",
                    sync_status=SyncStatus.COMPLETED,
                    last_sync_started_at=existing.last_sync_started_at,
                    last_sync_completed_at=completed_at,
                    last_sync_error=None,
                    last_channel_resolved_at=existing.last_channel_resolved_at,
                    last_discovery_at=existing.last_discovery_at,
                )
            )
        return {
            "synced_count": synced_count,
            "created_count": created_count,
            "updated_count": updated_count,
            "completed_at": completed_at.isoformat(),
        }

    def upsert_followed_artists(self, artists: list[Artist]) -> list[Artist]:
        for artist in artists:
            self.artist_repository.upsert(artist)
        return artists


class ChannelDiscoveryService:
    def __init__(self, artist_repository: ArtistRepository) -> None:
        self.artist_repository = artist_repository

    def resolve(self, artist: Artist, resolver: Callable[[Artist], str | None]) -> Artist:
        channel_id = resolver(artist)
        now = utc_now()
        updated_artist = Artist(
            spotify_id=artist.spotify_id,
            name=artist.name,
            yt_channel_id=channel_id or artist.yt_channel_id,
            status=artist.status,
            sync_status=artist.sync_status,
            last_sync_started_at=artist.last_sync_started_at,
            last_sync_completed_at=artist.last_sync_completed_at,
            last_sync_error=artist.last_sync_error,
            last_channel_resolved_at=now if channel_id else artist.last_channel_resolved_at,
            last_discovery_at=artist.last_discovery_at,
        )
        self.artist_repository.upsert(updated_artist)
        return updated_artist


class VideoDiscoveryService:
    def __init__(self, candidate_repository: CandidateRepository) -> None:
        self.candidate_repository = candidate_repository

    def discover_for_artist(
        self,
        artist: Artist,
        run: ArtistSyncRun,
        provider: Callable[[Artist, int], list[CandidateDiscoveryPayload]],
        days: int,
    ) -> list[VideoCandidate]:
        now = utc_now()
        candidates = []
        for item in provider(artist, days):
            candidate = VideoCandidate(
                candidate_id=f"{artist.spotify_id}:{item.video_id}",
                spotify_id=artist.spotify_id,
                video_id=item.video_id,
                channel_id=artist.yt_channel_id,
                title=item.title,
                source_url=item.source_url,
                source_kind=run.source_kind,
                status=CandidateStatus.DISCOVERED,
                ingestion_status=SyncStatus.COMPLETED,
                published_at=item.published_at,
                first_seen_at=now,
                last_seen_at=now,
                discovery_run_id=run.run_id,
            )
            self.candidate_repository.upsert(candidate)
            candidates.append(candidate)
        return candidates


class CandidateCatalogService:
    def __init__(
        self,
        artist_repository: ArtistRepository,
        artist_sync_run_repository: ArtistSyncRunRepository,
        candidate_repository: CandidateRepository,
        providers: Phase3Providers,
    ) -> None:
        self.artist_repository = artist_repository
        self.artist_sync_run_repository = artist_sync_run_repository
        self.candidate_repository = candidate_repository
        self.providers = providers
        self.artist_sync_service = ArtistSyncService(artist_repository, artist_sync_run_repository)
        self.channel_discovery_service = ChannelDiscoveryService(artist_repository)
        self.video_discovery_service = VideoDiscoveryService(candidate_repository)

    def sync_followed_artists(self, trigger: str = "manual") -> dict:
        return self.artist_sync_service.sync_followed_artists(
            provider=self.providers.followed_artists_lookup,
            trigger=trigger,
        )

    def list_artists(self, filters: ArtistListFilters) -> tuple[list[dict], int]:
        artists = [artist for artist in self.artist_repository.list_all() if artist.status == "active"]
        if filters.query:
            needle = filters.query.strip().lower()
            artists = [
                artist
                for artist in artists
                if needle in artist.name.lower() or needle in artist.spotify_id.lower()
            ]
        if filters.sync_status:
            artists = [
                artist for artist in artists if artist.sync_status.value == filters.sync_status
            ]

        candidate_count_by_artist = {
            artist.spotify_id: len(self._available_candidates_for_artist(artist.spotify_id))
            for artist in artists
        }

        if filters.sort == "last_synced_desc":
            artists.sort(
                key=lambda artist: (
                    artist.last_sync_completed_at or datetime.min,
                    artist.name.lower(),
                ),
                reverse=True,
            )
        elif filters.sort == "last_synced_asc":
            artists.sort(
                key=lambda artist: (
                    artist.last_sync_completed_at or datetime.min,
                    artist.name.lower(),
                ),
            )
        elif filters.sort == "candidate_count_desc":
            artists.sort(
                key=lambda artist: (
                    candidate_count_by_artist.get(artist.spotify_id, 0),
                    artist.name.lower(),
                ),
                reverse=True,
            )
        elif filters.sort == "candidate_count_asc":
            artists.sort(
                key=lambda artist: (
                    candidate_count_by_artist.get(artist.spotify_id, 0),
                    artist.name.lower(),
                ),
            )
        elif filters.sort == "sync_status_desc":
            artists.sort(
                key=lambda artist: (
                    self._sync_status_sort_rank(artist.sync_status),
                    artist.name.lower(),
                ),
                reverse=True,
            )
        elif filters.sort == "sync_status_asc":
            artists.sort(
                key=lambda artist: (
                    self._sync_status_sort_rank(artist.sync_status),
                    artist.name.lower(),
                ),
            )
        elif filters.sort == "name_desc":
            artists.sort(key=lambda artist: artist.name.lower(), reverse=True)
        else:
            artists.sort(key=lambda artist: artist.name.lower())

        total = len(artists)
        page_items = self._paginate(artists, filters.page, filters.page_size)
        return [self._artist_to_dto(artist) for artist in page_items], total

    def list_candidates(self, artist_id: str, filters: CandidateListFilters) -> tuple[list[dict], int]:
        candidates = self.candidate_repository.list_for_artist(artist_id)
        if filters.status:
            candidates = [
                candidate for candidate in candidates if candidate.status.value == filters.status
            ]

        total = len(candidates)
        page_items = self._paginate(candidates, filters.page, filters.page_size)
        return [self._candidate_to_dto(candidate) for candidate in page_items], total

    def resync_artist(self, artist_id: str, days: int = 14, trigger: str = "manual") -> dict:
        artist = self.artist_repository.get(artist_id)
        if artist is None:
            raise KeyError(artist_id)

        overall_started_at = utc_now()
        in_progress_artist = Artist(
            spotify_id=artist.spotify_id,
            name=artist.name,
            yt_channel_id=artist.yt_channel_id,
            status=artist.status,
            sync_status=SyncStatus.PROCESSING,
            last_sync_started_at=overall_started_at,
            last_sync_completed_at=artist.last_sync_completed_at,
            last_sync_error=None,
            last_channel_resolved_at=artist.last_channel_resolved_at,
            last_discovery_at=artist.last_discovery_at,
        )
        self.artist_repository.upsert(in_progress_artist)

        try:
            resolved_artist, channel_run = self._resolve_channel(
                in_progress_artist,
                trigger=trigger,
            )
            if not resolved_artist.yt_channel_id:
                raise RuntimeError("No YouTube channel could be resolved for this artist.")

            candidates, rss_run = self._discover_candidates(
                resolved_artist,
                days=days,
                trigger=trigger,
            )
            completed_at = utc_now()
            updated_artist = Artist(
                spotify_id=resolved_artist.spotify_id,
                name=resolved_artist.name,
                yt_channel_id=resolved_artist.yt_channel_id,
                status=resolved_artist.status,
                sync_status=SyncStatus.COMPLETED,
                last_sync_started_at=overall_started_at,
                last_sync_completed_at=completed_at,
                last_sync_error=None,
                last_channel_resolved_at=resolved_artist.last_channel_resolved_at,
                last_discovery_at=completed_at,
            )
            self.artist_repository.upsert(updated_artist)
            return {
                "run_id": rss_run.run_id,
                "artist_id": updated_artist.spotify_id,
                "status": rss_run.status.value,
                "discovered_count": rss_run.discovered_count,
                "started_at": overall_started_at.isoformat(),
                "completed_at": completed_at.isoformat(),
                "channel_run_id": channel_run.run_id,
                "discovery_run_id": rss_run.run_id,
            }
        except Exception as exc:
            completed_at = utc_now()
            failed_artist = Artist(
                spotify_id=in_progress_artist.spotify_id,
                name=in_progress_artist.name,
                yt_channel_id=in_progress_artist.yt_channel_id,
                status=in_progress_artist.status,
                sync_status=SyncStatus.FAILED,
                last_sync_started_at=overall_started_at,
                last_sync_completed_at=completed_at,
                last_sync_error=str(exc),
                last_channel_resolved_at=in_progress_artist.last_channel_resolved_at,
                last_discovery_at=in_progress_artist.last_discovery_at,
            )
            self.artist_repository.upsert(failed_artist)
            raise

    def refresh_active_artists(
        self,
        days: int = 14,
        limit: int | None = None,
        trigger: str = "system",
    ) -> dict:
        artists = [artist for artist in self.artist_repository.list_all() if artist.status == "active"]
        if limit is not None:
            artists = artists[:limit]

        refreshed = 0
        failed = 0
        failures: list[dict] = []
        for artist in artists:
            try:
                self.resync_artist(artist.spotify_id, days=days, trigger=trigger)
                refreshed += 1
            except Exception as exc:
                failed += 1
                failures.append({"artist_id": artist.spotify_id, "error": str(exc)})
        return {
            "requested": len(artists),
            "refreshed": refreshed,
            "failed": failed,
            "failures": failures,
        }

    def _resolve_channel(self, artist: Artist, trigger: str) -> tuple[Artist, ArtistSyncRun]:
        run = ArtistSyncRun(
            run_id=str(uuid.uuid4()),
            spotify_id=artist.spotify_id,
            source_kind="youtube_channel",
            status=SyncStatus.PROCESSING,
            started_at=utc_now(),
            trigger=trigger,
        )
        self.artist_sync_run_repository.create(run)
        try:
            resolved_artist = self.channel_discovery_service.resolve(artist, self.providers.channel_lookup)
            completed_run = ArtistSyncRun(
                run_id=run.run_id,
                spotify_id=run.spotify_id,
                source_kind=run.source_kind,
                status=SyncStatus.COMPLETED if resolved_artist.yt_channel_id else SyncStatus.FAILED,
                started_at=run.started_at,
                completed_at=utc_now(),
                failure_reason=(
                    None if resolved_artist.yt_channel_id else "No YouTube channel could be resolved for this artist."
                ),
                retry_count=run.retry_count,
                discovered_count=1 if resolved_artist.yt_channel_id else 0,
                trigger=run.trigger,
            )
            self.artist_sync_run_repository.update(completed_run)
            if not resolved_artist.yt_channel_id:
                raise RuntimeError(completed_run.failure_reason or "Channel resolution failed.")
            return resolved_artist, completed_run
        except Exception as exc:
            failed_run = ArtistSyncRun(
                run_id=run.run_id,
                spotify_id=run.spotify_id,
                source_kind=run.source_kind,
                status=SyncStatus.FAILED,
                started_at=run.started_at,
                completed_at=utc_now(),
                failure_reason=str(exc),
                retry_count=run.retry_count + 1,
                discovered_count=0,
                trigger=run.trigger,
            )
            self.artist_sync_run_repository.update(failed_run)
            raise

    def _discover_candidates(
        self,
        artist: Artist,
        days: int,
        trigger: str,
    ) -> tuple[list[VideoCandidate], ArtistSyncRun]:
        run = ArtistSyncRun(
            run_id=str(uuid.uuid4()),
            spotify_id=artist.spotify_id,
            source_kind="youtube_rss",
            status=SyncStatus.PROCESSING,
            started_at=utc_now(),
            trigger=trigger,
        )
        self.artist_sync_run_repository.create(run)
        try:
            candidates = self.video_discovery_service.discover_for_artist(
                artist,
                run,
                self.providers.candidate_lookup,
                days,
            )
            completed_run = ArtistSyncRun(
                run_id=run.run_id,
                spotify_id=run.spotify_id,
                source_kind=run.source_kind,
                status=SyncStatus.COMPLETED,
                started_at=run.started_at,
                completed_at=utc_now(),
                failure_reason=None,
                retry_count=run.retry_count,
                discovered_count=len(candidates),
                trigger=run.trigger,
            )
            self.artist_sync_run_repository.update(completed_run)
            return candidates, completed_run
        except Exception as exc:
            failed_run = ArtistSyncRun(
                run_id=run.run_id,
                spotify_id=run.spotify_id,
                source_kind=run.source_kind,
                status=SyncStatus.FAILED,
                started_at=run.started_at,
                completed_at=utc_now(),
                failure_reason=str(exc),
                retry_count=run.retry_count + 1,
                discovered_count=0,
                trigger=run.trigger,
            )
            self.artist_sync_run_repository.update(failed_run)
            raise

    def _artist_to_dto(self, artist: Artist) -> dict:
        candidates = self.candidate_repository.list_for_artist(artist.spotify_id)
        available_candidates = self._available_candidates(candidates)
        runs = self.artist_sync_run_repository.list_for_artist(artist.spotify_id)
        latest_run = next(iter(runs), None)
        latest_candidate = available_candidates[0] if available_candidates else None
        source_health = self._build_source_health(runs)
        partial_failure = any(
            source_state["status"] == SyncStatus.FAILED.value for source_state in source_health.values()
        ) and bool(candidates)
        return {
            "artist_id": artist.spotify_id,
            "name": artist.name,
            "status": artist.status,
            "youtube_channel_id": artist.yt_channel_id,
            "sync_status": artist.sync_status.value,
            "last_sync_started_at": self._iso(artist.last_sync_started_at),
            "last_sync_completed_at": self._iso(artist.last_sync_completed_at),
            "last_sync_error": artist.last_sync_error,
            "candidate_count": len(available_candidates),
            "partial_failure": partial_failure,
            "empty_state": len(available_candidates) == 0,
            "retry_metadata": {
                "can_resync": True,
                "latest_retry_count": latest_run.retry_count if latest_run else 0,
                "latest_failure_reason": latest_run.failure_reason if latest_run else None,
            },
            "source_health": source_health,
            "latest_candidate": self._candidate_to_dto(latest_candidate) if latest_candidate else None,
            "latest_run": {
                "run_id": latest_run.run_id,
                "status": latest_run.status.value,
                "source_kind": latest_run.source_kind,
                "discovered_count": latest_run.discovered_count,
                "failure_reason": latest_run.failure_reason,
                "started_at": self._iso(latest_run.started_at),
                "completed_at": self._iso(latest_run.completed_at),
            }
            if latest_run
            else None,
        }

    def _available_candidates_for_artist(self, artist_id: str) -> list[VideoCandidate]:
        return self._available_candidates(self.candidate_repository.list_for_artist(artist_id))

    @staticmethod
    def _available_candidates(candidates: list[VideoCandidate]) -> list[VideoCandidate]:
        return [
            candidate
            for candidate in candidates
            if candidate.status == CandidateStatus.DISCOVERED
        ]

    @staticmethod
    def _candidate_to_dto(candidate: VideoCandidate | None) -> dict | None:
        if candidate is None:
            return None
        return {
            "candidate_id": candidate.candidate_id,
            "video_id": candidate.video_id,
            "title": candidate.title,
            "status": candidate.status.value,
            "ingestion_status": candidate.ingestion_status.value,
            "channel_id": candidate.channel_id,
            "source_url": candidate.source_url,
            "source_kind": candidate.source_kind,
            "published_at": CandidateCatalogService._iso(candidate.published_at),
            "first_seen_at": CandidateCatalogService._iso(candidate.first_seen_at),
            "last_seen_at": CandidateCatalogService._iso(candidate.last_seen_at),
            "discovery_run_id": candidate.discovery_run_id,
            "failure_reason": candidate.failure_reason,
        }

    @staticmethod
    def _build_source_health(runs: list[ArtistSyncRun]) -> dict[str, dict]:
        latest_by_source: dict[str, ArtistSyncRun] = {}
        for run in runs:
            latest_by_source.setdefault(run.source_kind, run)
        return {
            source_kind: {
                "status": run.status.value,
                "retry_count": run.retry_count,
                "failure_reason": run.failure_reason,
                "started_at": CandidateCatalogService._iso(run.started_at),
                "completed_at": CandidateCatalogService._iso(run.completed_at),
                "discovered_count": run.discovered_count,
            }
            for source_kind, run in latest_by_source.items()
        }

    @staticmethod
    def _paginate(items: list, page: int, page_size: int) -> list:
        offset = (page - 1) * page_size
        return items[offset : offset + page_size]

    @staticmethod
    def _iso(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None

    @staticmethod
    def _sync_status_sort_rank(status: SyncStatus) -> int:
        order = {
            SyncStatus.FAILED: 0,
            SyncStatus.PARTIAL: 1,
            SyncStatus.PROCESSING: 2,
            SyncStatus.PENDING: 3,
            SyncStatus.COMPLETED: 4,
        }
        return order.get(status, 99)
