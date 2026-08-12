"""admin_factory: profile.type -> PlatformAdmin instance.

Plain if/elif, mirroring gateway/connectors/__init__.py's connector_factory
style rather than a plugin registry — same rationale: few backends, and an
explicit chain is easier to read than indirection for a set this small.
"""

from gateway.admin.base import PlatformAdmin
from gateway.admin.config import AdminConfigError, AdminProfile
from gateway.admin.mattermost_admin import MattermostAdmin
from gateway.admin.rocketchat_admin import RocketChatAdmin


def admin_factory(profile: AdminProfile) -> PlatformAdmin:
    if profile.type == "rocketchat":
        return RocketChatAdmin(profile)
    elif profile.type == "mattermost":
        return MattermostAdmin(profile)
    raise AdminConfigError(f"Profile '{profile.name}': unsupported type {profile.type!r}")
