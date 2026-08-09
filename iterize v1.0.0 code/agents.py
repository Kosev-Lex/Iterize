import agents_core as _core
from agents_enhancements import install as _install
from module_wrapper_utils import export_core as _export
_install(_core.__dict__)
_export(globals(), _core)
# Explicit exports for IDE/static-analysis support.
EvolutionLog = _core.EvolutionLog
AgentSpecStore = _core.AgentSpecStore
Orchestrator = _core.Orchestrator
OrchestratorWindow = _core.OrchestratorWindow
OrchestratorStopped = _core.OrchestratorStopped
LiveViewWindow = _core.LiveViewWindow
RoleWorkspace = _core.RoleWorkspace
resolve_agent_cfg = _core.resolve_agent_cfg
traceback_focus = _core.traceback_focus
mission_spec_path = _core.mission_spec_path
load_mission_spec = _core.load_mission_spec
splice = _core.splice
AGENTS_DIR = _core.AGENTS_DIR
INSTRUCTIONS_FILE = _core.INSTRUCTIONS_FILE
