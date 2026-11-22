"""Phase 8C entry point that enables confirmation-gated Supervisor safe actions.

The existing service implementation stays canonical. This bootstrap only replaces
the Supervisor/backend classes and status wrapper before starting the service.
"""
from __future__ import annotations

from . import service
from .supervisor_actions import (
    ActionSupervisorBackend,
    ActionSupervisorEngine,
    action_aware_extend,
)


def main() -> None:
    service.SupervisorBackend = ActionSupervisorBackend
    service.SupervisorEngine = ActionSupervisorEngine
    service.extend_private_handler = action_aware_extend(service.extend_private_handler)
    service.main()


if __name__ == "__main__":
    main()
