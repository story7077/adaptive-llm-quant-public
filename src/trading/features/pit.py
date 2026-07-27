from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime
from zoneinfo import ZoneInfo

from trading.features.models import AdjustedPriceObservation

NEW_YORK = ZoneInfo("America/New_York")


def historical_cutoff(session_date: date, reference_cutoff: datetime) -> datetime:
    local_reference = reference_cutoff.astimezone(NEW_YORK)
    local_cutoff = datetime.combine(
        session_date,
        local_reference.timetz().replace(tzinfo=None),
        tzinfo=NEW_YORK,
    )
    return local_cutoff.astimezone(reference_cutoff.tzinfo)


def select_session_marks(
    observations: Iterable[AdjustedPriceObservation],
    *,
    reference_cutoff: datetime,
) -> dict[str, dict[date, AdjustedPriceObservation]]:
    """Select the latest revision available at the same historical clock.

    This reconstructs each historical 15:44 ET (or caller-selected clock) mark
    instead of leaking a later close/revision into a past rolling feature.
    """

    selected: dict[str, dict[date, AdjustedPriceObservation]] = {}
    for observation in observations:
        if observation.event_time > reference_cutoff or observation.available_at > reference_cutoff:
            continue
        cutoff = historical_cutoff(observation.session_date, reference_cutoff)
        if observation.event_time > cutoff or observation.available_at > cutoff:
            continue
        by_date = selected.setdefault(observation.symbol, {})
        current = by_date.get(observation.session_date)
        if current is None or (
            observation.event_time,
            observation.available_at,
            observation.source_record_id,
        ) > (
            current.event_time,
            current.available_at,
            current.source_record_id,
        ):
            by_date[observation.session_date] = observation
    return selected


def common_sessions(
    panel: dict[str, dict[date, AdjustedPriceObservation]],
    symbols: Iterable[str],
) -> list[date]:
    requested = list(symbols)
    if not requested or any(symbol not in panel for symbol in requested):
        return []
    sessions = set(panel[requested[0]])
    for symbol in requested[1:]:
        sessions.intersection_update(panel[symbol])
    return sorted(sessions)
