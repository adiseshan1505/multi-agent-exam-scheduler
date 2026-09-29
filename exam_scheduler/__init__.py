"""Multi-agent university exam scheduling & conflict resolution.

Three agent types cooperate through a message bus:

* ExamRequestAgent  - one per course exam, asks for a slot and negotiates.
* ResourceAgent     - RoomAgent (one per room) and InvigilatorAgent (one per
                      faculty member); owns the true availability of a resource.
* CoordinatorAgent  - single central scheduler that runs a CSP backtracking
                      search over its *beliefs* about resources.
"""

from .simulation import Simulation
from .scenarios import SCENARIOS, build_scenario

__all__ = ["Simulation", "SCENARIOS", "build_scenario"]
