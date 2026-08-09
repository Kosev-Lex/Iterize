import verify_core as _core
from verify_enhancements import install as _install
from module_wrapper_utils import export_core as _export
_install(_core.__dict__)
_export(globals(), _core)

# Explicit exports for IDE/static-analysis support.
VerifyTab = _core.VerifyTab
Verifier = _core.Verifier
VerifierStopped = _core.VerifierStopped
iter_designations = _core.iter_designations
safe_name = _core.safe_name
INSTR_DIR = _core.INSTR_DIR
VERIF_SUB = _core.VERIF_SUB
