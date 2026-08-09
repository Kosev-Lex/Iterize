"""Iterize IDE by JL Kosev-Lex. Copyright (c) 2026. Iterize.org
This program is hereby made available as open source software under Apache 2.0 license terms.
"""

import os
import sys
import tkinter as tk
import pyedit_core as _core
from ui_enhancements import install as _install
from module_wrapper_utils import export_core as _export


def resource_path(relative_path):
    if hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), relative_path)


_install(_core.__dict__)
_export(globals(), _core)

if __name__ == "__main__":
    root = tk.Tk()

    root.iconbitmap(resource_path("it3_result.ico"))

    _core.IDE(
        root,
        sys.argv[1] if len(sys.argv) > 1 else None
    )

    root.mainloop()