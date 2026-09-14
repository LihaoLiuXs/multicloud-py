"""Cloud-agnostic pubsub client package.

The user-facing layer: ``TopicClient`` and ``SubscriptionClient`` plus the
value objects callers pass to their operations. Provider-specific code lives
in sibling packages (``pubsub_aws``, ``pubsub_gcp``, ``pubsub_ali``) and is
hidden behind the registry.
"""

from multicloud.pubsub.pubsub_client.batcher import Batcher
from multicloud.pubsub.pubsub_client.client import (
    SubscriptionClient,
    TopicClient,
)
from multicloud.pubsub.pubsub_client.driver import (
    SUBSCRIPTION_REGISTRY,
    TOPIC_REGISTRY,
    AckID,
    AckInfo,
    BaseSubscription,
    BaseTopic,
    GetAttributeResult,
    Message,
    MessageValidator,
)

__all__ = [
    # User-facing clients
    "TopicClient",
    "SubscriptionClient",
    # User-facing value objects
    "Message",
    "AckID",
    "GetAttributeResult",
]

# Note: Internal classes (BaseTopic, BaseSubscription, registries, etc.) are
# imported above for intra-package use but NOT included in __all__ since
# they're not part of the public API. Tests and advanced users can still
# import them directly if needed:
#   from multicloud.pubsub.pubsub_client.driver import BaseTopic
