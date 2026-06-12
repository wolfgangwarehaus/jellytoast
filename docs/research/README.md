# docs/research/

Design docs for work that's still in motion. Once a feature ships and
settles, its research doc is deleted from the tree — **every removed doc
remains in git history** (`git log --diff-filter=D --summary -- docs/research/`
lists them; code comments citing a removed path refer to that history).
The 2026-06-12 doc boil-down removed ~23 shipped-status docs this way.

| Doc | Status |
| --- | --- |
| `distribution_channels_2026-06-12.md` | ACTIVE — decision input for the post-v0.1.0 channels (MS Store, chaotic-AUR, skips) |
| `community_launch_2026-06-12.md` | ACTIVE — the launch playbook, executes after v0.1.0 |
| `portable_blur.md` | REFERENCE — per-desktop blur support; linked from the user guide + blur code |
| `audio_output_routing.md` | REFERENCE — device picker / ALSA-direct subsystem design |
