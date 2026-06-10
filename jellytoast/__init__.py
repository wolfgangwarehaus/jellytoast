# jellytoast package root. Deliberately empty of imports: the GUI app
# lives in jellytoast.app (imported by the `jellytoast` entry point and
# `python -m jellytoast`), and pulling it in here would drag Qt into
# every `from jellytoast.x import y` — tests and dev tools import
# submodules directly.
