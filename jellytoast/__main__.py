"""`python -m jellytoast` — same path as the `jellytoast` gui-script.

Kept to a bare trampoline: `jellytoast.app` does real work at import
time (Qt setup), so nothing here should import it except to launch.
"""

from jellytoast.app import main

if __name__ == "__main__":
    main()
