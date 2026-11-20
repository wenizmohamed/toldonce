"""ToldOnce: a lesson graph for coding agents, stored in Relate.

Graph model
-----------
(:Correction {id, said, at, session})-[:TAUGHT]->(:Lesson {id, text, created, status})
(:Correction)-[:REPEATS]->(:Lesson)            a correction the agent should not have needed
(:Lesson)-[:APPLIES_TO]->(:Project|:Tool|:Task {name})
(:Project)-[:USES]->(:Tool)                    learned from the repo, so tool lessons follow you
(:Lesson)-[:SUPERSEDES]->(:Lesson)             newer guidance replaces older
(:Lesson)-[:CONTRADICTS]->(:Lesson)            unresolved conflict, surfaced to the agent
(:Recall {id, at, project, prompt})-[:SURFACED {rank, score}]->(:Lesson)
"""
from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import relate

RECALL_CYPHER = (Path(__file__).with_name("recall.cypher")).read_text()
SCOPE_LABELS = {"project": "Project", "tool": "Tool", "task": "Task"}
DEFAULT_DB = os.environ.get("TOLDONCE_DB", str(Path.home() / ".toldonce" / "lessons.rl"))

# Weights for recall ranking. Kept in one place so the README can quote them.
W_PROJECT, W_TOOL, W_TASK, W_GLOBAL, W_TEXT, W_REPEAT = 3.0, 2.0, 2.0, 0.5, 4.0, 0.5

_WORD = re.compile(r"[a-z0-9][a-z0-9_.+]*")
_STOP = set("a an the to of in on for and or is are be do don't dont not never always use with it this that i you we".split())


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 1}


def _similar(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


def _ft_query(text: str) -> str:
    # Lucene-ish OR query from the meaningful words; empty string disables full-text.
    return " OR ".join(sorted(_tokens(text))[:24])


def _short_id() -> str:
    return uuid.uuid4().hex[:8]


@dataclass
class Hit:
    id: str
    text: str
    score: float
    why: list[str] = field(default_factory=list)
    scopes: list[str] = field(default_factory=list)
    repeats: int = 0
    replaced: int = 0
    conflicts: list[str] = field(default_factory=list)


class LessonGraph:
    def __init__(self, path: str = DEFAULT_DB):
        if path != ":memory:":
            Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = relate.connect(str(Path(path).expanduser()) if path != ":memory:" else path)
        self._schema()

    # -- plumbing ---------------------------------------------------------
    def q(self, cypher: str, **params):
        return self.db.execute(cypher, params).fetchall()

    def close(self):
        self.db.close()

    def _schema(self):
        for label in ("Lesson", "Correction", "Recall"):
            self.q(f"CREATE CONSTRAINT {label.lower()}_id IF NOT EXISTS FOR (n:{label}) REQUIRE n.id IS UNIQUE")
        for label in SCOPE_LABELS.values():
            self.q(f"CREATE CONSTRAINT {label.lower()}_name IF NOT EXISTS FOR (n:{label}) REQUIRE n.name IS UNIQUE")
        self.q("CREATE FULLTEXT INDEX lesson_text IF NOT EXISTS FOR (l:Lesson) ON EACH [l.text]")

    @staticmethod
    def _label(kind: str) -> str:
        if kind not in SCOPE_LABELS:
            raise ValueError(f"scope kind must be one of {sorted(SCOPE_LABELS)}, got {kind!r}")
        return SCOPE_LABELS[kind]

    # -- writing ----------------------------------------------------------
    def note_project(self, project: str, tools: list[str]):
        """Record what a project uses, so tool-scoped lessons reach it."""
        self.q("MERGE (:Project {name: $p})", p=project)
        for t in tools:
            self.q("MERGE (p:Project {name: $p}) MERGE (t:Tool {name: $t}) MERGE (p)-[:USES]->(t)", p=project, t=t)

    def find_repeat(self, text: str, scopes: list[tuple[str, str]], threshold: float = 0.5) -> str | None:
        """An active lesson that already says this, in an overlapping (or global) scope."""
        ft = _ft_query(text)
        if not ft:
            return None
        rows = self.q(
            """MATCH (l:Lesson) SEARCH l IN (FULLTEXT INDEX lesson_text FOR $ft LIMIT 10) SCORE AS s
               WHERE l.status = 'active' AND NOT EXISTS { (:Lesson)-[:SUPERSEDES]->(l) }
               RETURN l.id AS id, l.text AS text,
                      [(l)-[:APPLIES_TO]->(x) | [toLower(labels(x)[0]), x.name]] AS scopes""",
            ft=ft,
        )
        want = {(k, n) for k, n in scopes}
        best, best_sim = None, threshold
        for r in rows:
            have = {tuple(x) for x in r["scopes"]}
            if want and have and not (want & have):
                continue
            sim = _similar(text, r["text"])
            if sim >= best_sim:
                best, best_sim = r["id"], sim
        return best

    def learn(self, said: str, lesson: str | None = None, scopes: list[tuple[str, str]] = (),
              session: str | None = None, supersedes: str | None = None, detect_repeat: bool = True) -> dict:
        """Turn a correction into a lesson. Returns {'lesson': id, 'repeat': bool}."""
        text = (lesson or said).strip()
        scopes = list(scopes)
        cid = _short_id()
        self.q("CREATE (:Correction {id: $id, said: $said, at: datetime(), session: $s})",
               id=cid, said=said, s=session or "")

        existing = self.find_repeat(text, scopes) if detect_repeat and not supersedes else None
        if existing:
            self.q("MATCH (c:Correction {id: $c}), (l:Lesson {id: $l}) MERGE (c)-[:REPEATS]->(l)", c=cid, l=existing)
            self._attach(existing, scopes)
            return {"lesson": existing, "repeat": True, "correction": cid}

        lid = _short_id()
        self.q("CREATE (:Lesson {id: $id, text: $t, created: datetime(), status: 'active'})", id=lid, t=text)
        self.q("MATCH (c:Correction {id: $c}), (l:Lesson {id: $l}) MERGE (c)-[:TAUGHT]->(l)", c=cid, l=lid)
        self._attach(lid, scopes)
        if supersedes:
            self.supersede(lid, supersedes)
        return {"lesson": lid, "repeat": False, "correction": cid}

    def _attach(self, lid: str, scopes):
        for kind, name in scopes:
            label = self._label(kind)
            self.q(f"MATCH (l:Lesson {{id: $l}}) MERGE (s:{label} {{name: $n}}) MERGE (l)-[:APPLIES_TO]->(s)",
                   l=lid, n=name)

    def supersede(self, new: str, old: str):
        r = self.q("MATCH (n:Lesson {id: $n}), (o:Lesson {id: $o}) MERGE (n)-[:SUPERSEDES]->(o) "
                   "WITH n, o OPTIONAL MATCH (n)-[c:CONTRADICTS]-(o) DELETE c RETURN count(*) AS k", n=new, o=old)
        if not r or not r[0]["k"]:
            raise KeyError(f"lesson {new} or {old} not found")

    def contradict(self, a: str, b: str):
        self.q("MATCH (a:Lesson {id: $a}), (b:Lesson {id: $b}) MERGE (a)-[:CONTRADICTS]->(b)", a=a, b=b)

    def retire(self, lid: str):
        self.q("MATCH (l:Lesson {id: $l}) SET l.status = 'retired'", l=lid)

    # -- reading ----------------------------------------------------------
    def recall(self, project: str | None = None, tools: list[str] = (), prompt: str = "",
               limit: int = 8, log: bool = True) -> list[Hit]:
        """Lessons that apply here, ranked by how they connect to this context."""
        project = project or ""
        tools = list(tools)
        text = prompt.lower()
        ft = _ft_query(prompt)

        text_scores: dict[str, float] = {}
        if ft:
            for r in self.q("MATCH (l:Lesson) SEARCH l IN (FULLTEXT INDEX lesson_text FOR $ft LIMIT 50) SCORE AS s "
                            "RETURN l.id AS id, s", ft=ft):
                text_scores[r["id"]] = r["s"]
        top = max(text_scores.values(), default=0) or 1.0

        rows = self.q(
            RECALL_CYPHER,
            project=project, tools=tools, text=text,
        )

        hits: list[Hit] = []
        for r in rows:
            score, why = 0.0, []
            if r["hereProject"]:
                score += W_PROJECT; why.append(f"project {project}")
            for t in r["tools"]:
                score += W_TOOL; why.append(f"tool {t}")
            for k in r["tasks"]:
                score += W_TASK; why.append(f"task {k}")
            if r["nScopes"] == 0:
                score += W_GLOBAL; why.append("global")
            ts = text_scores.get(r["id"], 0.0) / top
            if ts:
                score += W_TEXT * ts; why.append(f"text {ts:.2f}")
            score += W_REPEAT * min(r["repeats"], 4)
            if r["repeats"]:
                why.append(f"told {r['repeats'] + 1}x")
            hits.append(Hit(r["id"], r["text"], round(score, 3), why, r["scopes"], r["repeats"], r["replaced"],
                            r["conflicts"]))

        hits.sort(key=lambda h: (-h.score, h.id))
        hits = hits[:limit]
        if log and hits:
            rid = _short_id()
            self.q("CREATE (:Recall {id: $id, at: datetime(), project: $p, prompt: $t})", id=rid, p=project,
                   t=prompt[:500])
            for i, h in enumerate(hits, 1):
                self.q("MATCH (r:Recall {id: $r}), (l:Lesson {id: $l}) CREATE (r)-[:SURFACED {rank: $i, score: $s}]->(l)",
                       r=rid, l=h.id, i=i, s=h.score)
        return hits

    def lessons(self, include_inactive: bool = False):
        return self.q(
            """MATCH (l:Lesson)
               WITH l, EXISTS { (:Lesson)-[:SUPERSEDES]->(l) } AS superseded
               WHERE $all OR (l.status = 'active' AND NOT superseded)
               RETURN l.id AS id, l.text AS text, l.status AS status, superseded,
                      [(l)-[:APPLIES_TO]->(x) | toLower(labels(x)[0]) + ':' + x.name] AS scopes,
                      COUNT { (:Correction)-[:REPEATS]->(l) } AS repeats,
                      toString(l.created) AS created
               ORDER BY created""",
            all=include_inactive,
        )

    def history(self, lid: str):
        """The supersede chain and every correction behind a lesson."""
        chain = self.q(
            """MATCH p = (l:Lesson {id: $id})-[:SUPERSEDES*0..]->(o:Lesson)
               RETURN o.id AS id, o.text AS text, length(p) AS depth ORDER BY depth""", id=lid)
        said = self.q(
            """MATCH (c:Correction)-[r:TAUGHT|REPEATS]->(:Lesson {id: $id})
               RETURN c.said AS said, type(r) AS kind, toString(c.at) AS at ORDER BY at""", id=lid)
        return chain, said

    def stats(self):
        r = self.q(
            """RETURN COUNT { (:Lesson) } AS lessons,
                      COUNT { (:Correction) } AS corrections,
                      COUNT { (:Correction)-[:REPEATS]->() } AS repeats,
                      COUNT { ()-[:SUPERSEDES]->() } AS superseded,
                      COUNT { ()-[:CONTRADICTS]->() } AS conflicts,
                      COUNT { (:Recall) } AS recalls,
                      COUNT { ()-[:SURFACED]->() } AS surfaced""")[0]
        r["most_repeated"] = self.q(
            """MATCH (c:Correction)-[:REPEATS]->(l:Lesson)
               RETURN l.id AS id, l.text AS text, count(c) + 1 AS told ORDER BY told DESC, id LIMIT 5""")
        return r

    def export_scoped_pack(self, project: str, fmt: str = "claude", exclude_conflicts: bool = True) -> dict:
        """Compile a conflict-free, zero-leak .claude/rules/ or AGENTS.md pack for one project."""
        if fmt not in ("claude", "agents"):
            raise ValueError(f"format must be 'claude' or 'agents', got {fmt!r}")
        proj_tools = [
            r["name"]
            for r in self.q("MATCH (:Project {name: $p})-[:USES]->(t:Tool) RETURN t.name AS name ORDER BY name", p=project)
        ]
        rows = self.q(
            """MATCH (l:Lesson)
               WHERE l.status = 'active' AND NOT EXISTS { (:Lesson)-[:SUPERSEDES]->(l) }
               WITH l,
                 EXISTS { (l)-[:APPLIES_TO]->(:Project {name: $project}) } AS hereProject,
                 EXISTS { (l)-[:APPLIES_TO]->(p:Project) WHERE p.name <> $project } AS otherProject,
                 [(l)-[:APPLIES_TO]->(t:Tool) WHERE EXISTS { (:Project {name: $project})-[:USES]->(t) } | t.name] AS tools,
                 [(l)-[:APPLIES_TO]->(k:Task) | k.name] AS tasks,
                 COUNT { (l)-[:APPLIES_TO]->() } AS nScopes,
                 COUNT { (l)-[:APPLIES_TO]->(:Tool) } AS nTools,
                 COUNT { (:Correction)-[:REPEATS]->(l) } AS repeats,
                 COUNT { (l)-[:SUPERSEDES*1..]->(:Lesson) } AS replaced,
                 [(l)-[:CONTRADICTS]-(o:Lesson) WHERE o.status = 'active' AND NOT EXISTS { (:Lesson)-[:SUPERSEDES]->(o) } | o.id] AS conflicts,
                 [(l)-[:APPLIES_TO]->(x) | toLower(labels(x)[0]) + ':' + x.name] AS scopes
               WHERE (hereProject OR NOT otherProject)
                 AND (nTools = 0 OR size(tools) > 0 OR hereProject)
               RETURN l.id AS id, l.text AS text, hereProject, tools, tasks, nScopes,
                      repeats, replaced, conflicts, scopes
               ORDER BY hereProject DESC, size(tools) DESC, repeats DESC, id""",
            project=project,
        )
        audit = self.q(
            """RETURN COUNT { (l:Lesson) WHERE l.status = 'active' AND NOT EXISTS { (:Lesson)-[:SUPERSEDES]->(l) }
                               AND EXISTS { (l)-[:APPLIES_TO]->(p:Project) WHERE p.name <> $project }
                               AND NOT EXISTS { (l)-[:APPLIES_TO]->(:Project {name: $project}) } } AS other_project_excluded,
                      COUNT { (l:Lesson) WHERE EXISTS { (:Lesson)-[:SUPERSEDES]->(l) } } AS superseded_excluded,
                      COUNT { (l:Lesson) WHERE l.status <> 'active' } AS retired_excluded""",
            project=project,
        )[0]

        active_rows = [r for r in rows if not (exclude_conflicts and r["conflicts"])]
        quarantined = [r for r in rows if exclude_conflicts and r["conflicts"]]

        proj_rules = [r for r in active_rows if r["hereProject"]]
        tool_rules = [r for r in active_rows if not r["hereProject"] and r["tools"]]
        task_rules = [r for r in active_rows if not r["hereProject"] and not r["tools"] and r["tasks"]]
        global_rules = [r for r in active_rows if r["nScopes"] == 0]

        def _prov(r):
            parts = [f"id={r['id']}"]
            if r["hereProject"]:
                parts.append(f"path=({r['id']}:Lesson)-[:APPLIES_TO]->({project}:Project)")
            elif r["tools"]:
                parts.append(f"path=({project}:Project)-[:USES]->({','.join(r['tools'])}:Tool)<-[:APPLIES_TO]-({r['id']}:Lesson)")
            elif r["tasks"]:
                parts.append(f"path=({r['id']}:Lesson)-[:APPLIES_TO]->({','.join(r['tasks'])}:Task)")
            else:
                parts.append("scope=global")
            if r["repeats"]:
                parts.append(f"told={r['repeats'] + 1}x")
            if r["replaced"]:
                parts.append(f"replaces={r['replaced']}")
            if r["conflicts"]:
                parts.append(f"conflicts={','.join(r['conflicts'])}")
            return f"<!-- toldonce: {' | '.join(parts)} -->"

        def _render_section(title, items):
            if not items:
                return []
            out = [f"## {title}", ""]
            for r in items:
                suffix = f" *(told {r['repeats'] + 1}x)*" if r["repeats"] else ""
                if r["tasks"] and not title.startswith("Task"):
                    suffix += f" `[task:{','.join(r['tasks'])}]`"
                if r["conflicts"] and not exclude_conflicts:
                    suffix += f" **[CONFLICTS with {', '.join(r['conflicts'])}: ask before choosing]**"
                out.append(f"- {r['text']}{suffix}")
                out.append(f"  {_prov(r)}")
            out.append("")
            return out

        lines: list[str] = []
        if fmt == "claude":
            filename = f".claude/rules/toldonce-{project}.md"
            lines.extend([
                "---",
                f"description: ToldOnce scoped Relate graph rules for {project} (conflict-free, zero cross-project leaks)",
                f"project: {project}",
                f"tools: [{', '.join(proj_tools)}]",
                f"generated_by: toldonce (Relate by Neo4j)",
                "---",
                "",
                f"# ToldOnce Scoped Rules: `{project}`",
                "",
                f"<!-- toldonce-audit: project={project} | active={len(active_rows)} | quarantined_conflicts={len(quarantined)} | other_projects_excluded={audit['other_project_excluded']} | superseded_excluded={audit['superseded_excluded']} -->",
                "",
            ])
        else:
            filename = "AGENTS.md"
            lines.extend([
                f"# AGENTS.md (Scoped for `{project}`)",
                "",
                f"> Generated live from Relate graph by ToldOnce. Zero cross-project leaks (`{audit['other_project_excluded']}` other-project rules excluded), zero stale rules (`{audit['superseded_excluded']}` superseded archived), `{len(quarantined)}` conflicting rules quarantined.",
                f"> Fingerprinted tools via `(:Project)-[:USES]->(:Tool)`: {', '.join(f'`{t}`' for t in proj_tools) or '(none)'}.",
                "",
                f"<!-- toldonce-audit: project={project} | active={len(active_rows)} | quarantined_conflicts={len(quarantined)} | other_projects_excluded={audit['other_project_excluded']} | superseded_excluded={audit['superseded_excluded']} -->",
                "",
            ])

        lines.extend(_render_section(f"Project Rules (`{project}`)", proj_rules))
        lines.extend(_render_section("Inherited Tool Rules (via `-[:USES]->`)", tool_rules))
        lines.extend(_render_section("Task-Triggered Rules", task_rules))
        lines.extend(_render_section("Global Engineering Rules", global_rules))

        if quarantined:
            lines.extend([
                "## Open Conflicts (Quarantined - Ask Developer Before Applying)",
                "",
                "<!-- toldonce-conflict-guard: omitted from active directives to preserve a conflict-free instruction pack -->",
                "",
            ])
            for r in quarantined:
                lines.append(f"- [ ] `{r['id']}` (conflicts with `{', '.join(r['conflicts'])}`): {r['text']}")
                lines.append(f"  {_prov(r)}")
            lines.append("")

        return {
            "project": project,
            "format": fmt,
            "filename": filename,
            "tools": proj_tools,
            "active_count": len(active_rows),
            "quarantined_count": len(quarantined),
            "other_project_excluded": audit["other_project_excluded"],
            "superseded_excluded": audit["superseded_excluded"],
            "retired_excluded": audit["retired_excluded"],
            "active_ids": [r["id"] for r in active_rows],
            "quarantined_ids": [r["id"] for r in quarantined],
            "content": "\n".join(lines).rstrip() + "\n",
        }


def _lit(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_lit(x) for x in v) + "]"
    if type(v).__name__ == "Temporal":
        return f"datetime({_lit(str(v.text) if hasattr(v, 'text') else str(v))})"
    return "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"


def export_cypher(g: "LessonGraph", recalls: bool = False) -> str:
    """The whole graph as a replayable Cypher script (used by the browser demo)."""
    out = ["CREATE FULLTEXT INDEX lesson_text IF NOT EXISTS FOR (l:Lesson) ON EACH [l.text];"]
    nodes = g.q("MATCH (n) WHERE $r OR NOT n:Recall RETURN elementId(n) AS k, labels(n) AS labels, properties(n) AS props", r=recalls)
    keys = {}
    for i, n in enumerate(nodes):
        keys[n["k"]] = i
        props = ", ".join(f"{k}: {_lit(v)}" for k, v in sorted(n["props"].items()))
        out.append(f"CREATE (:{':'.join(n['labels'])} {{_k: {i}{', ' + props if props else ''}}});")
    rels = g.q("MATCH (a)-[r]->(b) RETURN elementId(a) AS a, elementId(b) AS b, type(r) AS t, properties(r) AS props")
    for r in rels:
        if r["a"] not in keys or r["b"] not in keys:
            continue
        props = ", ".join(f"{k}: {_lit(v)}" for k, v in sorted(r["props"].items()))
        out.append(f"MATCH (a {{_k: {keys[r['a']]}}}), (b {{_k: {keys[r['b']]}}}) "
                   f"CREATE (a)-[:{r['t']}{' {' + props + '}' if props else ''}]->(b);")
    out.append("MATCH (n) REMOVE n._k;")
    return "\n".join(out) + "\n"


def detect_tools(root: str | os.PathLike = ".") -> list[str]:
    """Cheap repo fingerprinting: marker files -> tool names."""
    root = Path(root)
    markers = {
        ".git": "git", "package.json": "node", "pnpm-lock.yaml": "pnpm", "yarn.lock": "yarn",
        "pyproject.toml": "python", "requirements.txt": "python", "Cargo.toml": "rust", "go.mod": "go",
        "Dockerfile": "docker", "docker-compose.yml": "docker", "vercel.json": "vercel",
        "next.config.js": "nextjs", "next.config.mjs": "nextjs", "next.config.ts": "nextjs",
        "tailwind.config.js": "tailwind", "tailwind.config.ts": "tailwind", "prisma": "prisma",
        ".github/workflows": "github-actions", "Makefile": "make",
    }
    return sorted({tool for marker, tool in markers.items() if (root / marker).exists()})
