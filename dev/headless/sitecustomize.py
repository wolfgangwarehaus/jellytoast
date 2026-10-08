"""Headless-run shim: put this directory on PYTHONPATH for an offscreen app run.

The offscreen QPA has no system tray, and jellytoast's boot shows a MODAL
"No system tray" warning when there isn't one — which blocks a headless run
(and its test bridge) forever. Pretend a tray exists. Dev-only; never ships.
See dev/PLASMA_MOBILE_PLAN.md → "Headless verification recipe".
"""

from PySide6.QtWidgets import QSystemTrayIcon

QSystemTrayIcon.isSystemTrayAvailable = staticmethod(lambda: True)
