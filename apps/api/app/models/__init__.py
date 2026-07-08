"""All SQLAlchemy models. Importing this package registers every table on
Base.metadata (alembic env.py and create_all rely on that)."""
from __future__ import annotations

from ..db import Base
from .ai import (
    EMBED_DIM,
    AIAgent,
    AIAgentUsage,
    AIPointBalance,
    AIPointPrice,
    Intent,
    KBChunk,
    KBCollection,
    KBDocument,
    TranslationUsage,
)
from .base import uuid7
from .billing import (
    BalanceLedger,
    BillingOrder,
    Invoice,
    StripeEvent,
    WorkspaceBalance,
)
from .channels import CHANNEL_TYPES, ChannelAccount, DeviceBridge, Widget
from .contacts import (
    ChannelIdentity,
    Contact,
    ContactMerge,
    ContactMergeCandidate,
    ContactNote,
    ContactOrder,
    VisitorEvent,
)
from .conversations import (
    Conversation,
    ConversationAssignment,
    ConversationRead,
    ConversationSession,
)
from .flows import (
    Flow,
    FlowCategory,
    FlowSession,
    FlowSessionStep,
    FlowStatsDaily,
    FlowStatsUser,
    FlowTemplate,
    FlowTrigger,
    FlowTriggerLog,
    FlowVersion,
    KeywordDict,
    KeywordDictItem,
)
from .marketing import (
    PARTITIONED_TABLES as MARKETING_PARTITIONED_TABLES,
)
from .marketing import (
    Broadcast,
    BroadcastRecipient,
    BroadcastRun,
    EdmCampaign,
    MsgTemplate,
    Segment,
    SmsSignature,
    SplitLink,
    SplitLinkClick,
)
from .members import (
    MemberDailyStats,
    MemberGroup,
    MemberGroupMember,
    MemberShift,
    Role,
    User,
    WorkspaceMember,
)
from .messaging import (
    PARTITIONED_TABLES,
    File,
    Message,
    MessageDedup,
    MessageTranslation,
    TranslationCache,
)
from .misc import (
    ApiToken,
    AuditLog,
    ContactTag,
    ConversationTag,
    CustomFieldDefinition,
    EventRow,
    Material,
    QuickReply,
    QuickReplyFolder,
    SavedView,
    Tag,
    Timer,
    WebhookDelivery,
    WebhookSubscription,
)
from .platform import PlatformSettings
from .reports import (
    AgentPresenceSession,
    AggAdsDaily,
    AggAgentHourly,
    AggConversationsHourly,
    AggCustomersDaily,
    AggMessagesHourly,
    ConversationAttribution,
    ReportAiSummary,
    ReportExport,
    ReportShare,
    RollupWatermark,
)
from .tenancy import (
    AIPointsLedger,
    LLMProfileRow,
    MacActivity,
    Plan,
    Subscription,
    UsageCounter,
    Workspace,
)

__all__ = [
    "Base",
    "uuid7",
    "CHANNEL_TYPES",
    "PARTITIONED_TABLES",
    "EMBED_DIM",
    # tenancy
    "Plan",
    "Workspace",
    "Subscription",
    "UsageCounter",
    "AIPointsLedger",
    "MacActivity",
    "LLMProfileRow",
    # members
    "User",
    "Role",
    "WorkspaceMember",
    "MemberGroup",
    "MemberGroupMember",
    "MemberShift",
    "MemberDailyStats",
    # channels
    "ChannelAccount",
    "DeviceBridge",
    "Widget",
    # contacts
    "Contact",
    "ChannelIdentity",
    "ContactMerge",
    "ContactMergeCandidate",
    "ContactNote",
    "ContactOrder",
    "VisitorEvent",
    # conversations
    "Conversation",
    "ConversationSession",
    "ConversationAssignment",
    "ConversationRead",
    # messaging
    "Message",
    "MessageDedup",
    "File",
    "MessageTranslation",
    "TranslationCache",
    # misc
    "Tag",
    "ContactTag",
    "ConversationTag",
    "AuditLog",
    "SavedView",
    "QuickReplyFolder",
    "QuickReply",
    "Material",
    "WebhookSubscription",
    "WebhookDelivery",
    "ApiToken",
    "CustomFieldDefinition",
    "EventRow",
    "Timer",
    # flows (P2)
    "FlowCategory",
    "Flow",
    "FlowVersion",
    "FlowTrigger",
    "KeywordDict",
    "KeywordDictItem",
    "FlowSession",
    "FlowSessionStep",
    "FlowTriggerLog",
    "FlowStatsDaily",
    "FlowStatsUser",
    "FlowTemplate",
    # ai (P2)
    "AIAgent",
    "AIAgentUsage",
    "Intent",
    "AIPointPrice",
    "AIPointBalance",
    "TranslationUsage",
    "KBCollection",
    "KBDocument",
    "KBChunk",
    # marketing (P3)
    "MARKETING_PARTITIONED_TABLES",
    "Segment",
    "Broadcast",
    "BroadcastRun",
    "BroadcastRecipient",
    "MsgTemplate",
    "SmsSignature",
    "SplitLink",
    "SplitLinkClick",
    "EdmCampaign",
    # reports (P3)
    "AggMessagesHourly",
    "AggConversationsHourly",
    "AggAgentHourly",
    "AggCustomersDaily",
    "AggAdsDaily",
    "AgentPresenceSession",
    "ConversationAttribution",
    "ReportShare",
    "ReportExport",
    "ReportAiSummary",
    "RollupWatermark",
    # billing (P3)
    "WorkspaceBalance",
    "BalanceLedger",
    "BillingOrder",
    "StripeEvent",
    "Invoice",
    # platform (super-admin, tenant-agnostic)
    "PlatformSettings",
]
