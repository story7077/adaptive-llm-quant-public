from __future__ import annotations

from types import TracebackType

from sqlalchemy.orm import Session, sessionmaker

from trading.persistence.repositories import DomainEventRepository, SourceRecordRepository


class UnitOfWork:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self.session: Session | None = None
        self.events: DomainEventRepository
        self.sources: SourceRecordRepository

    def __enter__(self) -> UnitOfWork:
        self.session = self._session_factory()
        self.events = DomainEventRepository(self.session)
        self.sources = SourceRecordRepository(self.session)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self.session is None:
            return
        if exc_type is not None:
            self.session.rollback()
        self.session.close()

    def commit(self) -> None:
        self._require_session().commit()

    def rollback(self) -> None:
        self._require_session().rollback()

    def _require_session(self) -> Session:
        if self.session is None:
            raise RuntimeError("UnitOfWork is not active")
        return self.session

