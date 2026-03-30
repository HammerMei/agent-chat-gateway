"""Structured agent execution errors used across backends and core delivery.

Backends should translate raw CLI / HTTP / protocol failures into typed errors
here so the core layer can map them to stable user-facing messages without
guessing from free-form exception strings.
"""

from __future__ import annotations


class AgentExecutionError(RuntimeError):
    """Base class for backend execution failures."""


class AgentRateLimitedError(AgentExecutionError):
    """The backend rejected the request due to quota / usage / rate limits."""


class AgentPermissionError(AgentExecutionError):
    """The backend could not proceed because a permission/policy check failed."""


class AgentUnavailableError(AgentExecutionError):
    """The backend service is temporarily unavailable."""


class AgentProtocolError(AgentExecutionError):
    """The backend returned malformed or unexpected protocol output."""
