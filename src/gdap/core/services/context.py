"""Service context.

One instance = one transaction + one principal + one tenant. Services are built lazily so a
request that only lists datasets never constructs the AI stack.
"""

from __future__ import annotations

from functools import cached_property
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from gdap.core.config import Settings
from gdap.core.contracts import Principal
from gdap.governance.audit import AuditTrail
from gdap.governance.lineage import LineageTracker
from gdap.governance.policy import PolicyEngine, PolicySettings
from gdap.storage.repositories import OrganizationRepository

if TYPE_CHECKING:  # pragma: no cover
    from gdap.core.container import Platform
    from gdap.core.services.agent_service import AgentService
    from gdap.core.services.alert_service import AlertService
    from gdap.core.services.analysis_service import AnalysisService
    from gdap.core.services.dataset_service import DatasetService
    from gdap.core.services.governance_service import GovernanceService
    from gdap.core.services.job_service import JobService
    from gdap.core.services.model_service import ModelService
    from gdap.core.services.pipeline_service import PipelineService
    from gdap.core.services.report_service import ReportService
    from gdap.core.services.source_service import SourceService


class ServiceContext:
    def __init__(self, *, platform: Platform, session: Session, principal: Principal) -> None:
        self.platform = platform
        self.session = session
        self.principal = principal

    # ------------------------------------------------------------------ ambient
    @property
    def settings(self) -> Settings:
        return self.platform.settings

    @property
    def org_id(self) -> str:
        return self.principal.org_id

    @cached_property
    def audit(self) -> AuditTrail:
        return AuditTrail(self.session, self.org_id)

    @cached_property
    def lineage(self) -> LineageTracker:
        return LineageTracker(self.session, self.org_id)

    @cached_property
    def policy(self) -> PolicyEngine:
        organization = OrganizationRepository(self.session).get(self.org_id)
        settings = PolicySettings.from_mapping(
            (organization.settings or {}).get("policy") if organization else None
        )
        return PolicyEngine(settings)

    # ------------------------------------------------------------------ services
    @cached_property
    def sources(self) -> SourceService:
        from gdap.core.services.source_service import SourceService

        return SourceService(self)

    @cached_property
    def datasets(self) -> DatasetService:
        from gdap.core.services.dataset_service import DatasetService

        return DatasetService(self)

    @cached_property
    def pipelines(self) -> PipelineService:
        from gdap.core.services.pipeline_service import PipelineService

        return PipelineService(self)

    @cached_property
    def jobs(self) -> JobService:
        from gdap.core.services.job_service import JobService

        return JobService(self)

    @cached_property
    def analyses(self) -> AnalysisService:
        from gdap.core.services.analysis_service import AnalysisService

        return AnalysisService(self)

    @cached_property
    def reports(self) -> ReportService:
        from gdap.core.services.report_service import ReportService

        return ReportService(self)

    @cached_property
    def alerts(self) -> AlertService:
        from gdap.core.services.alert_service import AlertService

        return AlertService(self)

    @cached_property
    def governance(self) -> GovernanceService:
        from gdap.core.services.governance_service import GovernanceService

        return GovernanceService(self)

    @cached_property
    def models(self) -> ModelService:
        from gdap.core.services.model_service import ModelService

        return ModelService(self)

    @cached_property
    def agents(self) -> AgentService:
        from gdap.core.services.agent_service import AgentService

        return AgentService(self)

    def with_principal(self, principal: Principal) -> ServiceContext:
        """Same transaction, different actor (used when a job runs on behalf of its creator)."""
        return ServiceContext(platform=self.platform, session=self.session, principal=principal)
