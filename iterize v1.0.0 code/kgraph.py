import kgraph_core as _core

from kgraph_enhancements import install as _install
from module_wrapper_utils import export_core as _export

_install(_core.__dict__)
_export(globals(), _core)

# Explicit exports for IDE/static-analysis support.
KGMemory = _core.KGMemory