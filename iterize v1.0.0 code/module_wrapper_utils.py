"""Export a patched core module without clobbering wrapper metadata."""
def export_core(namespace, core):
    skip = {"__name__", "__loader__", "__package__", "__spec__", "__file__", "__cached__"}
    namespace.update({k: v for k, v in vars(core).items() if k not in skip})
