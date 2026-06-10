"""Playback subsystems that live alongside the primary mpv handle in
``jellytoast/player_backend.py``. Lifted into a package so the Crossfader's
dual-handle plumbing (and any future sibling pieces) don't bloat
``player_backend.py`` past its single-responsibility waterline.
"""
