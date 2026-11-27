"""Minimal MCP server (stdio, JSON-RPC per line) exposing ToldOnce to any agent host.

Tools: recall, learn, supersede, history. No dependencies beyond relate-db.
Run: toldonce-mcp  (or python -m toldonce.mcp), with TOLDONCE_DB pointing at the store.
"""
from __future__ import annotations

import json
import sys

from .store import DEFAULT_DB, LessonGraph

TOOLS = [
    {"name": "recall", "description": "Lessons from past corrections that apply to the current request. "
                                      "Call before acting on a task.",
     "inputSchema": {"type": "object", "properties": {
         "prompt": {"type": "string"}, "project": {"type": "string"},
         "tools": {"type": "array", "items": {"type": "string"}}, "limit": {"type": "integer"}}}},
    {"name": "learn", "description": "Record a user correction as a lesson, scoped to project/tool/task. "
                                     "Repeats of an existing lesson are detected and counted.",
     "inputSchema": {"type": "object", "required": ["said"], "properties": {
         "said": {"type": "string"}, "lesson": {"type": "string"},
         "project": {"type": "string"}, "tool": {"type": "string"}, "task": {"type": "string"},
         "supersedes": {"type": "string"}}}},
    {"name": "supersede", "description": "Mark lesson `new` as replacing lesson `old`.",
     "inputSchema": {"type": "object", "required": ["new", "old"],
                     "properties": {"new": {"type": "string"}, "old": {"type": "string"}}}},
    {"name": "resolve_conflict", "description": "Resolve an active :CONTRADICTS conflict by keeping `winner` and superseding `loser`.",
     "inputSchema": {"type": "object", "required": ["winner", "loser"],
                     "properties": {"winner": {"type": "string"}, "loser": {"type": "string"}, "note": {"type": "string"}}}},
    {"name": "history", "description": "A lesson's supersede chain and the corrections behind it.",
     "inputSchema": {"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}}}},
    {"name": "import_markdown", "description": "Explode a flat CLAUDE.md, AGENTS.md, or .cursorrules markdown document into scoped Relate graph nodes, merging repeats and flagging contradictions.",
     "inputSchema": {"type": "object", "required": ["markdown"], "properties": {"markdown": {"type": "string"}}}},
    {"name": "profile_recall", "description": "Execute Relate native PROFILE on recall.cypher, returning relate.plan/v1 operator tree and parse/plan/execute millisecond phase telemetry.",
     "inputSchema": {"type": "object", "properties": {
         "prompt": {"type": "string"}, "project": {"type": "string"},
         "tools": {"type": "array", "items": {"type": "string"}}}}},
    {"name": "export_rules", "description": "Generate a conflict-free, zero-leak .claude/rules/, AGENTS.md, or .cursor/rules/*.mdc instruction pack with provenance comments for one project.",
     "inputSchema": {"type": "object", "required": ["project"], "properties": {
         "project": {"type": "string"}, "format": {"type": "string", "enum": ["claude", "agents", "cursor"]},
         "exclude_conflicts": {"type": "boolean"}}}},
]


def call(g: LessonGraph, name: str, args: dict):
    if name == "recall":
        hits = g.recall(args.get("project"), args.get("tools") or [], args.get("prompt", ""), args.get("limit", 8))
        return [h.__dict__ for h in hits]
    if name == "learn":
        scopes = [(k, args[k]) for k in ("project", "tool", "task") if args.get(k)]
        return g.learn(args["said"], args.get("lesson"), scopes, supersedes=args.get("supersedes"))
    if name == "supersede":
        g.supersede(args["new"], args["old"]); return {"ok": True}
    if name == "resolve_conflict":
        return g.resolve_conflict(args["winner"], args["loser"], note=args.get("note"))
    if name == "history":
        chain, said = g.history(args["id"]); return {"chain": chain, "corrections": said}
    if name == "import_markdown":
        return g.import_markdown(args["markdown"])
    if name == "profile_recall":
        return g.profile_recall(args.get("project"), args.get("tools") or [], args.get("prompt", ""))
    if name == "export_rules":
        return g.export_scoped_pack(
            args["project"],
            fmt=args.get("format", "claude"),
            exclude_conflicts=args.get("exclude_conflicts", True),
        )
    raise ValueError(f"unknown tool {name}")


def main(db: str = DEFAULT_DB):
    g = LessonGraph(db)
    for line in sys.stdin:
        if not line.strip():
            continue
        msg = json.loads(line)
        mid, method = msg.get("id"), msg.get("method")
        if mid is None:  # notification
            continue
        try:
            if method == "initialize":
                result = {"protocolVersion": msg.get("params", {}).get("protocolVersion", "2025-03-26"),
                          "capabilities": {"tools": {}}, "serverInfo": {"name": "toldonce", "version": "0.2.0"}}

            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                p = msg["params"]
                data = call(g, p["name"], p.get("arguments") or {})
                result = {"content": [{"type": "text", "text": json.dumps(data, default=str)}]}
            elif method == "ping":
                result = {}
            else:
                raise LookupError(f"method not found: {method}")
            reply = {"jsonrpc": "2.0", "id": mid, "result": result}
        except LookupError as e:
            reply = {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": str(e)}}
        except Exception as e:  # tool errors go back to the model, not a crash
            reply = {"jsonrpc": "2.0", "id": mid,
                     "result": {"isError": True, "content": [{"type": "text", "text": str(e)}]}}
        print(json.dumps(reply), flush=True)
    g.close()


if __name__ == "__main__":
    main()
