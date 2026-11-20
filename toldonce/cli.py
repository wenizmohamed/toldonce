"""toldonce command line."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from .store import DEFAULT_DB, LessonGraph, detect_tools

# Phrases that usually mean "you got it wrong, here is the rule". Conservative on purpose.
CORRECTION = re.compile(
    r"^\s*(no[,.!]|nope|stop\b|don'?t\b|do not\b|never\b|always\b|i told you|i said|again[,!]|wrong[,.!]|"
    r"please stop|why did you|you keep)", re.I)


def _scopes(a) -> list[tuple[str, str]]:
    out = []
    for kind in ("project", "tool", "task"):
        for name in getattr(a, kind) or []:
            out.append((kind, name))
    return out


def _print_hits(hits, as_json=False):
    if as_json:
        print(json.dumps([h.__dict__ for h in hits], indent=2))
        return
    if not hits:
        print("no lessons apply here")
        return
    for i, h in enumerate(hits, 1):
        extra = []
        if h.replaced:
            extra.append(f"replaces {h.replaced} older")
        if h.conflicts:
            extra.append("CONFLICTS " + ",".join(h.conflicts))
        print(f"{i}. [{h.id}] {h.text}")
        print(f"   score {h.score}  because {', '.join(h.why) or '-'}" + (f"  ({'; '.join(extra)})" if extra else ""))


def render_context(hits) -> str:
    """The block a coding agent sees before it acts."""
    if not hits:
        return ""
    lines = ["<toldonce>", "Lessons from earlier corrections that apply to this request. Follow them:"]
    for h in hits:
        note = f" (told {h.repeats + 1} times)" if h.repeats else ""
        conflict = f" [conflicts with {', '.join(h.conflicts)}: ask before choosing]" if h.conflicts else ""
        lines.append(f"- {h.text}{note}{conflict}  <!-- {h.id}: {', '.join(h.why)} -->")
    lines.append("</toldonce>")
    return "\n".join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(prog="toldonce", description="Tell your coding agent once.")
    p.add_argument("--db", default=DEFAULT_DB, help=f"Relate store (default {DEFAULT_DB}, env TOLDONCE_DB)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def scoped(sp):
        sp.add_argument("--project", action="append")
        sp.add_argument("--tool", action="append")
        sp.add_argument("--task", action="append")

    s = sub.add_parser("learn", help="record a correction as a lesson")
    s.add_argument("said", help="what you told the agent")
    s.add_argument("--lesson", help="the rule, if different from what you said")
    s.add_argument("--supersedes", help="lesson id this replaces")
    s.add_argument("--session")
    scoped(s)

    s = sub.add_parser("recall", help="lessons that apply to a context")
    s.add_argument("prompt", nargs="?", default="")
    s.add_argument("--project")
    s.add_argument("--tool", action="append", default=[])
    s.add_argument("--cwd", help="detect project + tools from this directory")
    s.add_argument("--limit", type=int, default=8)
    s.add_argument("--json", action="store_true")
    s.add_argument("--context", action="store_true", help="print the block an agent sees")

    s = sub.add_parser("list", help="active lessons")
    s.add_argument("--all", action="store_true")
    s.add_argument("--json", action="store_true")

    s = sub.add_parser("show", help="a lesson's history: supersede chain + corrections")
    s.add_argument("id")

    s = sub.add_parser("supersede", help="NEW replaces OLD")
    s.add_argument("new"); s.add_argument("old")
    s = sub.add_parser("contradict", help="mark two lessons as conflicting")
    s.add_argument("a"); s.add_argument("b")
    s = sub.add_parser("retire", help="switch a lesson off")
    s.add_argument("id")
    s = sub.add_parser("stats")
    s.add_argument("--json", action="store_true")

    s = sub.add_parser("export", help="dump the graph as a replayable Cypher script")
    s.add_argument("--recalls", action="store_true", help="include Recall log nodes")

    s = sub.add_parser("export-rules", help="export a scoped, conflict-free .claude/rules/ or AGENTS.md pack")
    s.add_argument("--project", required=True, help="target project name")
    s.add_argument("--format", dest="fmt", choices=["claude", "agents"], default="claude", help="pack format")
    s.add_argument("--include-conflicts", action="store_true", help="keep conflicted rules inline instead of quarantining")
    s.add_argument("--json", action="store_true", help="print JSON metadata + content")

    s = sub.add_parser("hook", help="Claude Code UserPromptSubmit hook (JSON on stdin)")
    s.add_argument("--capture", action="store_true", help="also learn from prompts that read as corrections")

    a = p.parse_args(argv)
    g = LessonGraph(a.db)
    try:
        return _run(a, g)
    finally:
        g.close()


def _run(a, g: LessonGraph):
    if a.cmd == "learn":
        r = g.learn(a.said, a.lesson, _scopes(a), a.session, a.supersedes)
        if r["repeat"]:
            print(f"already told: lesson {r['lesson']} (recorded as a repeat)")
        else:
            print(f"learned {r['lesson']}")
    elif a.cmd == "recall":
        project, tools = a.project, list(a.tool)
        if a.cwd:
            root = Path(a.cwd).resolve()
            project = project or root.name
            found = detect_tools(root)
            g.note_project(project, found)
            tools += found
        hits = g.recall(project, tools, a.prompt, a.limit)
        print(render_context(hits)) if a.context else _print_hits(hits, a.json)
    elif a.cmd == "list":
        rows = g.lessons(a.all)
        if a.json:
            print(json.dumps(rows, indent=2, default=str))
        for r in [] if a.json else rows:
            flag = "" if r["status"] == "active" and not r["superseded"] else "  (inactive)"
            told = f"  told {r['repeats'] + 1}x" if r["repeats"] else ""
            print(f"[{r['id']}] {r['text']}  {' '.join(r['scopes']) or 'global'}{told}{flag}")
    elif a.cmd == "show":
        chain, said = g.history(a.id)
        if not chain:
            print(f"no lesson {a.id}"); return 1
        for c in chain:
            print(("  " * c["depth"]) + ("" if c["depth"] == 0 else "replaces ") + f"[{c['id']}] {c['text']}")
        for s in said:
            print(f"  {s['at'][:19]}  {s['kind'].lower():8} \"{s['said']}\"")
    elif a.cmd == "supersede":
        g.supersede(a.new, a.old); print(f"{a.new} supersedes {a.old}")
    elif a.cmd == "contradict":
        g.contradict(a.a, a.b); print(f"{a.a} conflicts with {a.b}")
    elif a.cmd == "retire":
        g.retire(a.id); print(f"retired {a.id}")
    elif a.cmd == "stats":
        st = g.stats()
        if a.json:
            print(json.dumps(st, indent=2)); return 0
        print(f"{st['lessons']} lessons from {st['corrections']} corrections; "
              f"{st['repeats']} were repeats; {st['superseded']} superseded; {st['conflicts']} conflicts; "
              f"{st['recalls']} recalls surfaced {st['surfaced']} lessons")
        for m in st["most_repeated"]:
            print(f"  told {m['told']}x  [{m['id']}] {m['text']}")
    elif a.cmd == "export":
        from .store import export_cypher
        print(export_cypher(g, a.recalls), end="")
    elif a.cmd == "export-rules":
        pack = g.export_scoped_pack(a.project, fmt=a.fmt, exclude_conflicts=not a.include_conflicts)
        if a.json:
            print(json.dumps(pack, indent=2))
        else:
            print(pack["content"], end="")
    elif a.cmd == "hook":
        return _hook(a, g)
    return 0


def _hook(a, g: LessonGraph):
    """Claude Code UserPromptSubmit: stdout on exit 0 is added to the model's context."""
    try:
        event = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    prompt = event.get("prompt", "")
    root = Path(event.get("cwd") or ".").resolve()
    project, tools = root.name, detect_tools(root)
    g.note_project(project, tools)
    out = []
    if a.capture and CORRECTION.match(prompt) and len(prompt) < 400:
        r = g.learn(prompt, scopes=[("project", project)], session=event.get("session_id"))
        out.append(f"<toldonce-captured lesson=\"{r['lesson']}\" repeat=\"{str(r['repeat']).lower()}\"/>")
    block = render_context(g.recall(project, tools, prompt))
    if block:
        out.append(block)
    if out:
        print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
