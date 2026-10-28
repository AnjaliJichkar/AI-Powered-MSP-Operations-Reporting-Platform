"""SQLAlchemy models.

Importing this package registers every table on ``Base.metadata``, which is what
Alembic autogenerate reads.
"""

from app.db.base import Base
from app.models.audit_log import AuditLog
from app.models.automation import (
    AutomationAlertTicket,
    AutomationKillSwitch,
    AutomationNote,
    AutomationNoteMode,
    AutomationReport,
    AutomationRun,
    AutomationStep,
    AutomationSwitch,
    AutomationTriggerLog,
    AutomationTriggerSwitch,
    PracticeRecord,
)
from app.models.client import Client
from app.models.collected_event import CollectedEvent, CollectionCoverage
from app.models.conversation import Conversation, ConversationMessage
from app.models.coverage import CoverageExclusion, CoverageSnapshot
from app.models.device import Device, DeviceAgreement, DeviceUnresolved, DeviceVendorLink
from app.models.enums import (
    AuditEventType,
    AuditOutcome,
    ConnectorName,
    FeedbackRating,
    IdentityProvider,
    Interface,
    MessageRole,
    OrganisationKind,
    Role,
    UserStatus,
)
from app.models.labelling import LabellingAnswer, LabellingItem, LabellingSet
from app.models.organisation import Invite, Organisation, Session, User
from app.models.outcome import TicketOutcome
from app.models.report import ReportConfig, TbrReport
from app.models.shadow import (
    ShadowBatch,
    ShadowEnrichment,
    ShadowPrediction,
    ShadowRun,
    ShadowSample,
    ShadowSampleTicket,
)
from app.models.site import Site
from app.models.suggestion import BlindCheck, SuggestionReview, TicketSuggestion
from app.models.ticket import TicketPlan, TicketWorkspace
from app.models.tool_credential import ToolCredential
from app.models.tool_mapping import ToolMapping

__all__ = [
    "AuditEventType",
    "AuditLog",
    "AuditOutcome",
    "AutomationAlertTicket",
    "AutomationKillSwitch",
    "AutomationNote",
    "AutomationNoteMode",
    "AutomationReport",
    "AutomationRun",
    "AutomationStep",
    "AutomationSwitch",
    "AutomationTriggerLog",
    "AutomationTriggerSwitch",
    "Base",
    "BlindCheck",
    "Client",
    "CollectedEvent",
    "CollectionCoverage",
    "ConnectorName",
    "Conversation",
    "ConversationMessage",
    "CoverageExclusion",
    "CoverageSnapshot",
    "Device",
    "DeviceAgreement",
    "DeviceUnresolved",
    "DeviceVendorLink",
    "FeedbackRating",
    "IdentityProvider",
    "Interface",
    "Invite",
    "LabellingAnswer",
    "LabellingItem",
    "LabellingSet",
    "MessageRole",
    "Organisation",
    "OrganisationKind",
    "PracticeRecord",
    "ReportConfig",
    "Role",
    "Session",
    "ShadowBatch",
    "ShadowEnrichment",
    "ShadowPrediction",
    "ShadowRun",
    "ShadowSample",
    "ShadowSampleTicket",
    "Site",
    "SuggestionReview",
    "TbrReport",
    "TicketOutcome",
    "TicketPlan",
    "TicketSuggestion",
    "TicketWorkspace",
    "ToolCredential",
    "ToolMapping",
    "User",
    "UserStatus",
]
