"""Long-lived service objects shared by API routes (created once in the lifespan)."""

from dataclasses import dataclass

from newsintel.audit import AuditLog
from newsintel.config import Settings
from newsintel.index.repository import Repository
from newsintel.inference import InferenceClient
from newsintel.queue import JobPublisher
from newsintel.search.service import SearchService
from newsintel.storage import ObjectStore


@dataclass
class Services:
    settings: Settings
    repo: Repository
    store: ObjectStore
    publisher: JobPublisher
    inference: InferenceClient
    audit: AuditLog
    search: SearchService

    @classmethod
    def build(cls, settings: Settings) -> "Services":
        repo, inference = Repository(settings), InferenceClient(settings)
        return cls(
            settings=settings,
            repo=repo,
            store=ObjectStore(settings),
            publisher=JobPublisher(settings),
            inference=inference,
            audit=AuditLog(settings),
            search=SearchService(settings, repo, inference),
        )

    def close(self) -> None:
        self.publisher.close()
        self.inference.close()
