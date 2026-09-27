"""统一鉴权导入入口。

``XHSPcAuth`` and ``XHSCreatorAuth`` remain the platform-specific views used
by the lower-level APIs.  ``XHSUnifiedAuth`` is the normal user-facing entry:
one PC QR/SMS/cookie login produces both views from the shared server session,
while each view keeps its own browser-derived signing state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from xhs_utils.xhs_creator import (
    CREATOR_PARAMETER_SOURCES,
    CreatorB1RuntimeState,
    CreatorDeviceProfile,
    XHSCreatorAuth,
)

from xhs_utils.xhs_pc import (
    B1RuntimeState,
    PC_PARAMETER_SOURCES,
    PcDeviceProfile,
    XHSAuth,
    XHSPcAuth,
)
from xhs_utils.xhs_core import (
    CREATOR_PLATFORM_CONFIG,
    PC_PLATFORM_CONFIG,
    XHSPlatformConfig,
)


@dataclass
class XHSUnifiedAuth:
    """One login entry point exposing PC and Creator signing views.

    The two sites share authentication Cookies (``a1``, ``web_session``,
    ``gid`` and related server-issued values), but localStorage/sessionStorage
    are origin-scoped and their b1/MNS/X-s envelopes differ.  Consequently
    this object intentionally owns two lightweight Auth views rather than
    flattening their runtime state into one signer.
    """

    pc_auth: XHSPcAuth
    _creator_auth: Optional[XHSCreatorAuth] = field(
        default=None,
        init=False,
        repr=False,
    )
    _creator_proxies: Optional[dict] = field(
        default=None,
        init=False,
        repr=False,
    )

    @property
    def pc(self) -> XHSPcAuth:
        """Short alias for the PC API view."""
        return self.pc_auth

    @property
    def creator(self) -> XHSCreatorAuth:
        """Short alias for the Creator API view."""
        return self.creator_auth

    @property
    def creator_auth(self) -> XHSCreatorAuth:
        """Lazily initialize the Creator view on first Creator API use."""
        if self._creator_auth is None:
            self._creator_auth = XHSCreatorAuth.from_pc_auth(
                self.pc_auth,
                proxies=self._creator_proxies,
            )
        return self._creator_auth

    @classmethod
    def from_qrcode_login(
        cls,
        *,
        show_in_terminal: bool = True,
        proxies: Optional[dict] = None,
    ) -> 'XHSUnifiedAuth':
        """Scan once on the PC login page; initialize Creator lazily if used."""
        pc = XHSPcAuth.from_qrcode_login(
            show_in_terminal=show_in_terminal,
            proxies=proxies,
        )
        auth = cls(pc_auth=pc)
        # Keep the explicitly selected proxy for a later lazy Creator bridge.
        auth._creator_proxies = proxies
        return auth

    @classmethod
    def from_phone_login(
        cls,
        *,
        proxies: Optional[dict] = None,
    ) -> 'XHSUnifiedAuth':
        """Complete one PC SMS login and initialize the Creator view from it."""
        pc = XHSPcAuth.from_phone_login(proxies=proxies)
        auth = cls(pc_auth=pc)
        auth._creator_proxies = proxies
        return auth

    @classmethod
    def from_cookie(
        cls,
        cookies: Any,
        *,
        proxies: Optional[dict] = None,
    ) -> 'XHSUnifiedAuth':
        """Create both views from one complete authenticated Cookie header."""
        pc = XHSPcAuth.from_cookie(cookies, proxies=proxies)
        auth = cls(pc_auth=pc)
        auth._creator_proxies = proxies
        return auth

    def close(self) -> None:
        """Close both independent HTTP transports."""
        creator_client = getattr(self._creator_auth, 'http_client', None)
        pc_client = getattr(self.pc_auth, 'http_client', None)
        close_creator = getattr(self._creator_auth, 'close', None)
        close_pc = getattr(self.pc_auth, 'close', None)
        if callable(close_creator):
            close_creator()
        if pc_client is not creator_client and callable(close_pc):
            close_pc()

    def __enter__(self) -> 'XHSUnifiedAuth':
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

__all__ = [
    'PC_PARAMETER_SOURCES',
    'CREATOR_PARAMETER_SOURCES',
    'XHSAuth',
    'XHSPlatformConfig',
    'PC_PLATFORM_CONFIG',
    'CREATOR_PLATFORM_CONFIG',
    'XHSPcAuth',
    'XHSCreatorAuth',
    'B1RuntimeState',
    'PcDeviceProfile',
    'CreatorB1RuntimeState',
    'CreatorDeviceProfile',
    'XHSUnifiedAuth',
]
