"""Long-lived service objects shared by API routes (created once in the lifespan)."""

from dataclasses import dataclass

from newsintel.audit import AuditLog
from newsintel.config import Settings
from newsintel.index.repository import Repository
from newsintel.inference import InferenceClient
from newsintel.queue import JobPublisher
from newsintel.storage import ObjectStore


@dataclass
class Services:
    settings: Settings
    repo: Repository
    store: ObjectStore
    publisher: JobPublisher
    inference: InferenceClient
    audit: AuditLog

    @classmethod
    def build(cls, settings: Settings) -> "Services":
        return cls(
            settings=settings,
            repo=Repository(settings),
            store=ObjectStore(settings),
            publisher=JobPublisher(settings),
            inference=InferenceClient(settings),
            audit=AuditLog(settings),
        )

    def close(self) -> None:
        self.publisher.close()
        self.inference.close()
