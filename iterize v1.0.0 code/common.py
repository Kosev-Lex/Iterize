import common_core as _core

from common_enhancements import install as _install
from module_wrapper_utils import export_core as _export

# Apply the secure API/provider enhancements to the real implementation.
_install(_core.__dict__)

# Preserve every public common_core export.
_export(globals(), _core)

# Explicit exports for IDE/static-analysis support.
api_chat = _core.api_chat
api_complete = _core.api_complete
load_api_config = _core.load_api_config
save_api_config = _core.save_api_config

API_CONFIG_PATH = _core.API_CONFIG_PATH
PROVIDER_PRESETS = _core.PROVIDER_PRESETS

atomic_write = _core.atomic_write
atomic_write_json = _core.atomic_write_json
backup_damaged = _core.backup_damaged

attach_context_menu = _core.attach_context_menu
persist_geometry = _core.persist_geometry
UIState = _core.UIState

extract_json = _core.extract_json
now_iso = _core.now_iso
sha1_text = _core.sha1_text

locate_chain = _core.locate_chain
segment_for = _core.segment_for

gate_compile = _core.gate_compile
run_harness_sandbox = _core.run_harness_sandbox
promote = _core.promote
resolve_python_interpreter = _core.resolve_python_interpreter