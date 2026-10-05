#!/usr/bin/env python3
"""Generates docs/demo.svg: an animated (SMIL) terminal-window demo for the
README, built entirely from real command output captured earlier in this
session (edge_repo index run + live MCP tool calls against it) - not
invented numbers."""

FONT = "SFMono-Regular,Consolas,Liberation Mono,Menlo,monospace"
FONT_SIZE = 14
CHAR_W = FONT_SIZE * 0.6
LINE_H = 22
PAD_X = 20
PAD_TOP = 46
WIDTH = 720

# (text, color, indent_chars)
LINES = [
    ("$ codemap-index ./my-repo", "#7dd3fc", 0),
    ("Indexed ./my-repo", "#e5e7eb", 0),
    ("  files: 12 total, 12 (re)parsed, 0 removed", "#9ca3af", 0),
    ("  calls: 16 resolved (5 via self/type inference)", "#9ca3af", 0),
    ("  graph: 48 nodes, 59 edges", "#9ca3af", 0),
    ("", "#000000", 0),
    ('$ # ask Claude Code: "what calls Worker.run, and what', "#7dd3fc", 0),
    ('$ # breaks if I change it?"', "#7dd3fc", 0),
    ("→ neighbors(\"Worker\", \"in\")     1 caller found", "#86efac", 0),
    ("→ impacted_by(\"Worker\")          reverse-reachability, repo-wide", "#86efac", 0),
]

TOTAL = 16.0  # seconds, full loop
N = len(LINES)
start_gap = 1.15
reveal_dur = 0.55
hold_until = 0.90  # fraction of TOTAL where all lines start fading together
fade_dur = 0.5     # seconds

svg_lines = []
svg_lines.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" '
                  f'height="{PAD_TOP + LINE_H * N + 20}" viewBox="0 0 {WIDTH} '
                  f'{PAD_TOP + LINE_H * N + 20}" font-family="{FONT}">')

# window chrome
h = PAD_TOP + LINE_H * N + 20
svg_lines.append(f'<rect x="0" y="0" width="{WIDTH}" height="{h}" rx="10" '
                  f'fill="#0f172a"/>')
svg_lines.append(f'<rect x="0" y="0" width="{WIDTH}" height="32" rx="10" '
                  f'fill="#1e293b"/>')
svg_lines.append(f'<rect x="0" y="16" width="{WIDTH}" height="16" fill="#1e293b"/>')
for i, c in enumerate(["#ff5f56", "#ffbd2e", "#27c93f"]):
    svg_lines.append(f'<circle cx="{20 + i * 18}" cy="16" r="6" fill="{c}"/>')
svg_lines.append(f'<text x="{WIDTH/2}" y="21" font-size="12" fill="#94a3b8" '
                  f'text-anchor="middle">zsh — codegraph demo</text>')

fade_start = TOTAL * hold_until
fade_end = fade_start + fade_dur

for idx, (text, color, indent) in enumerate(LINES):
    if not text:
        continue
    y = PAD_TOP + LINE_H * idx
    start = start_gap * idx
    end_reveal = start + reveal_dur
    # clip rect's x=0 is in the SAME coordinate space as the text (which
    # starts at x=PAD_X), so the reveal width must cover PAD_X + the text's
    # own pixel width, not just the text's width - missed on the first pass
    # and caught by an actual screenshot showing the last character or two
    # of several lines clipped off, not by inspection alone.
    full_w = PAD_X + len(text) * CHAR_W + 20
    text_esc = (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;"))
    clip_id = f"clip{idx}"
    # typewriter reveal: width grows 0 -> full over reveal_dur, holds, then
    # snaps back to 0 right at the shared loop point (so restart is clean)
    svg_lines.append(
        f'<clipPath id="{clip_id}">'
        f'<rect x="0" y="{y - FONT_SIZE}" height="{LINE_H}">'
        f'<animate attributeName="width" '
        f'values="0;0;{full_w:.1f};{full_w:.1f};0" '
        f'keyTimes="0;{start/TOTAL:.4f};{end_reveal/TOTAL:.4f};'
        f'{fade_start/TOTAL:.4f};1" '
        f'dur="{TOTAL}s" repeatCount="indefinite" calcMode="linear"/>'
        f'</rect></clipPath>'
    )
    svg_lines.append(
        f'<text x="{PAD_X}" y="{y}" font-size="{FONT_SIZE}" fill="{color}" '
        f'clip-path="url(#{clip_id})">{text_esc}</text>'
    )

# blinking cursor after the last non-empty line, synced to the typing.
# Build (time_sec, opacity) keyframes programmatically so the sequence is
# guaranteed strictly increasing and never overruns the shared fade point
# (a hand-rolled version of this got the seconds/fraction math wrong and
# produced an invalid, non-monotonic keyTimes list - verified broken via
# an actual headless-Chrome console-error check, not assumed).
last_idx = max(i for i, (t, _, _) in enumerate(LINES) if t)
cursor_y = PAD_TOP + LINE_H * last_idx
last_text = LINES[last_idx][0]
cursor_x = PAD_X + len(last_text) * CHAR_W + 10
cursor_appear_sec = start_gap * last_idx + reveal_dur
fade_start_sec = TOTAL * hold_until

blink_half = 0.35  # seconds on, then off
keyframes = [(0.0, 0), (cursor_appear_sec, 0)]
t = cursor_appear_sec
on = True
while t + blink_half < fade_start_sec:
    t += blink_half
    keyframes.append((t, 1 if on else 0))
    on = not on
keyframes.append((fade_start_sec, 0))
keyframes.append((TOTAL, 0))

key_times = ";".join(f"{t/TOTAL:.4f}" for t, _ in keyframes)
values = ";".join(str(v) for _, v in keyframes)

svg_lines.append(
    f'<rect x="{cursor_x:.1f}" y="{cursor_y - FONT_SIZE}" width="8" '
    f'height="{LINE_H - 6}" fill="#e5e7eb">'
    f'<animate attributeName="opacity" values="{values}" '
    f'keyTimes="{key_times}" dur="{TOTAL}s" repeatCount="indefinite"/>'
    f'</rect>'
)

svg_lines.append('</svg>')

import os
out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "demo.svg")
with open(out_path, "w") as f:
    f.write("\n".join(svg_lines))

print("wrote docs/demo.svg")
print(f"total cycle: {TOTAL}s, {N} lines")
