"""Proxy configuration shared by every provider.

Clients otherwise inherit whatever proxy the environment advertises, which is
wrong for traffic that must stay inside the network: a same-region storage call
routed through an egress proxy fails rather than going direct.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from multicloud.common.exceptions import InvalidArgumentException


@dataclass(frozen=True)
class ProxyConfig:
    """How a client should resolve its HTTP proxy.

    Attributes:
        endpoint: Proxy URL, such as ``http://proxy.example.net:8443``.
        use_environment_variable_proxy_values: Whether to honour ``HTTP_PROXY``
            and friends. True by default; set False to connect directly.

    Setting ``endpoint`` also stops the environment being consulted, so
    ``NO_PROXY`` no longer applies. One flag rather than the two some SDKs
    expose, because Python cannot disable the environment variables and the
    OS-level proxy settings separately.
    """

    endpoint: Optional[str] = None
    use_environment_variable_proxy_values: bool = True

    def __post_init__(self) -> None:
        if self.endpoint is None:
            return
        endpoint = self.endpoint.strip()
        if not endpoint:
            raise InvalidArgumentException(
                "proxy endpoint must not be blank; omit it to disable"
            )
        if "://" not in endpoint:
            raise InvalidArgumentException(
                f"proxy endpoint must include a scheme, got '{self.endpoint}'"
            )
        # Padding is not rejected, so keep the stripped form rather than one
        # that passes validation and then fails to proxy.
        object.__setattr__(self, "endpoint", endpoint)
