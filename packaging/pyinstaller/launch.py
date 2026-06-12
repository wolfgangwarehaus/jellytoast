"""PyInstaller entry script. The frozen binary can't use the
`jellytoast` console-script shim (that's a pip install artifact), so this
one-liner is the analysis root instead. Keep it logic-free — anything
that matters belongs in jellytoast.app.main() so the pip, deb, and
frozen launch paths stay identical.
"""

from jellytoast.app import main

main()
