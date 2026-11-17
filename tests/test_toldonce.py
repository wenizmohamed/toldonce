import json, subprocess, sys
from pathlib import Path
import pytest
from toldonce import LessonGraph


@pytest.fixture
def g(tmp_path):
    g = LessonGraph(str(tmp_path / "l.rl"))
    yield g
    g.close()


def ids(hits):
    return [h.id for h in hits]


def test_repeat_is_detected_not_duplicated(g):
    a = g.learn("no em dashes in client copy, use hyphens", scopes=[("task", "copy")])
    b = g.learn("don't use em dashes in copy, hyphens only", scopes=[("task", "copy")])
    assert b["repeat"] and b["lesson"] == a["lesson"]
    assert len(g.lessons()) == 1
    assert g.stats()["repeats"] == 1


def test_tool_lesson_follows_project_that_uses_tool(g):
    l = g.learn("run tests before committing", scopes=[("tool", "git")])["lesson"]
    g.note_project("shop", ["git", "node"])
    g.note_project("notes", [])
    assert l in ids(g.recall("shop", [], "fix the header"))
    assert l not in ids(g.recall("notes", [], "fix the header"))


def test_project_lessons_stay_in_their_project(g):
    l = g.learn("use pnpm here", scopes=[("project", "shop")])["lesson"]
    assert l in ids(g.recall("shop", [], "install deps"))
    assert l not in ids(g.recall("blog", [], "install deps"))


def test_task_lesson_needs_task_in_prompt(g):
    l = g.learn("squash before merge", scopes=[("task", "merge")])["lesson"]
    assert l in ids(g.recall("x", [], "please merge the branch"))
    assert l not in ids(g.recall("x", [], "rename a variable"))


def test_supersede_hides_old_and_keeps_history(g):
    old = g.learn("deploy with vercel --prod", scopes=[("tool", "vercel")])["lesson"]
    new = g.learn("deploy through the GitHub action, never vercel --prod by hand",
                  scopes=[("tool", "vercel")], supersedes=old)["lesson"]
    hits = g.recall("site", ["vercel"], "deploy it")
    assert new in ids(hits) and old not in ids(hits)
    assert next(h for h in hits if h.id == new).replaced == 1
    chain, said = g.history(new)
    assert [c["id"] for c in chain] == [new, old]


def test_conflicts_are_surfaced(g):
    a = g.learn("tabs for indentation", scopes=[("project", "p")])["lesson"]
    b = g.learn("two spaces for indentation", scopes=[("project", "p")])["lesson"]
    g.contradict(a, b)
    hits = {h.id: h for h in g.recall("p", [], "")}
    assert hits[a].conflicts == [b] and hits[b].conflicts == [a]


def test_recall_is_logged_as_provenance(g):
    g.learn("global rule: answer in English")
    g.recall("p", [], "hello")
    assert g.stats()["surfaced"] == 1


def test_store_persists(tmp_path):
    path = str(tmp_path / "l.rl")
    g = LessonGraph(path); g.learn("keep it", scopes=[("tool", "git")]); g.close()
    g = LessonGraph(path); assert len(g.lessons()) == 1; g.close()


def test_hook_injects_context_and_captures(tmp_path):
    repo = tmp_path / "shop"; (repo / ".git").mkdir(parents=True)
    db = str(tmp_path / "h.rl")
    def hook(prompt):
        ev = json.dumps({"prompt": prompt, "cwd": str(repo), "session_id": "s1"})
        return subprocess.run([sys.executable, "-m", "toldonce", "--db", db, "hook", "--capture"],
                              input=ev, capture_output=True, text=True, check=True).stdout
    out = hook("no, never force-push to main")
    assert "toldonce-captured" in out and "force-push" in out
    out = hook("ok now push the fix")
    assert "never force-push to main" in out and "<toldonce>" in out


def test_mcp_roundtrip(tmp_path):
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "learn", "arguments": {"said": "never commit .env files", "tool": "git"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "recall", "arguments": {"prompt": "commit this", "tools": ["git"]}}},
    ]
    env = {**__import__("os").environ, "TOLDONCE_DB": str(tmp_path / "m.rl")}
    out = subprocess.run([sys.executable, "-c", "import toldonce.mcp as m, os; m.main(os.environ['TOLDONCE_DB'])"],
                         input="\n".join(json.dumps(m) for m in msgs), capture_output=True, text=True,
                         env=env, check=True).stdout.splitlines()
    replies = [json.loads(l) for l in out]
    assert [r["id"] for r in replies] == [1, 2, 3, 4]
    assert {t["name"] for t in replies[1]["result"]["tools"]} >= {"recall", "learn"}
    hits = json.loads(replies[3]["result"]["content"][0]["text"])
    assert hits[0]["text"] == "never commit .env files"


def test_web_demo_uses_the_same_recall_cypher():
    root = Path(__file__).parent.parent
    assert (root / "web" / "recall.cypher").read_text() == (root / "toldonce" / "recall.cypher").read_text()


def test_export_replays_into_a_fresh_store(tmp_path):
    import relate
    from toldonce.store import export_cypher
    g = LessonGraph(str(tmp_path / "a.rl"))
    a = g.learn("never force-push to main", scopes=[("tool", "git")])["lesson"]
    g.learn("never force push to main!", scopes=[("tool", "git")])
    script = export_cypher(g); g.close()
    db = relate.connect(":memory:")
    for st in [s for s in script.split(";\n") if s.strip()]:
        db.execute(st)
    rows = db.execute("MATCH (:Correction)-[:REPEATS]->(l:Lesson) RETURN l.id AS id").fetchall()
    assert rows == [{"id": a}]
