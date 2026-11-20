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
    assert (root / "web" / "recall.cypher").read_bytes() == (root / "toldonce" / "recall.cypher").read_bytes()


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


def test_transitive_supersede_chain_keeps_only_head(g):
    v1 = g.learn("deploy with ftp upload", scopes=[("tool", "vercel")])["lesson"]
    v2 = g.learn("deploy with vercel --prod cli", scopes=[("tool", "vercel")], supersedes=v1)["lesson"]
    v3 = g.learn("deploy via github actions ci only", scopes=[("tool", "vercel")], supersedes=v2)["lesson"]
    hits = g.recall("site", ["vercel"], "deploy release")
    hit_ids = ids(hits)
    assert v3 in hit_ids and v2 not in hit_ids and v1 not in hit_ids
    assert next(h for h in hits if h.id == v3).replaced == 2
    chain, _ = g.history(v3)
    assert [c["id"] for c in chain] == [v3, v2, v1]


def test_multi_repo_tool_inheritance_and_zero_project_leak(g):
    g.note_project("storefront", ["git", "pnpm"])
    g.note_project("trading-bot", ["git", "python"])
    git_rule = g.learn("never force-push to main", scopes=[("tool", "git")])["lesson"]
    pnpm_rule = g.learn("use pnpm not npm", scopes=[("project", "storefront")])["lesson"]
    py_rule = g.learn("pin exact python package versions", scopes=[("tool", "python")])["lesson"]

    sf_ids = ids(g.recall("storefront", [], "install packages and push"))
    tb_ids = ids(g.recall("trading-bot", [], "install packages and push"))

    assert git_rule in sf_ids and pnpm_rule in sf_ids and py_rule not in sf_ids
    assert git_rule in tb_ids and py_rule in tb_ids and pnpm_rule not in tb_ids


def test_repeat_does_not_merge_across_disjoint_projects(g):
    a = g.learn("use strict types everywhere", scopes=[("project", "alpha")])
    b = g.learn("use strict types everywhere", scopes=[("project", "beta")])
    assert not b["repeat"] and b["lesson"] != a["lesson"]
    assert len(g.lessons()) == 2


def test_root_redirect_and_web_playground_assets_exist():
    root = Path(__file__).parent.parent
    idx = (root / "index.html").read_text(encoding="utf-8")
    web = (root / "web" / "index.html").read_text(encoding="utf-8")
    assert "./web/" in idx
    assert "recall.cypher" in web and "seed.cypher" in web
    assert "Interactive Relate Graph Visualizer" in web
    assert "Trace Drawer" in web
    assert "Interactive Live <em>Cypher</em> Console" in web
    assert "SUPERSEDES*1.. Audit Chain" in web
    assert "CONTRADICTS Conflict Finder" in web
    assert "Top Repeated Corrections" in web
    assert "Cross-Repo Tool Matrix" in web
    assert "1-Click Export to <code>.claude/rules/</code>" in web
    assert "Explode Into Relate Graph" in web
    assert "8-Query Live WASM Benchmark" in web


def test_eight_query_multi_repo_benchmark_zero_leak_on_demo_store(tmp_path):
    """Mirror the live 8-query WASM benchmark in Python: 100% precision, 0 cross-project/stale leaks."""
    import shutil
    demo_db = Path(__file__).parent.parent / "demo" / "lessons.rl"
    copy_db = tmp_path / "demo_copy.rl"
    shutil.copy2(demo_db, copy_db)
    g = LessonGraph(str(copy_db))
    try:
        cases = [
            ("storefront", "install dependencies and build app", "9dd0b4e9", {"34230a0d", "5fde76c1", "7cc562d0", "ed5bd42c"}, False),
            ("trading-bot", "update live strategy params", "34230a0d", {"9dd0b4e9", "1864a4ba", "7cc562d0", "ed5bd42c"}, False),
            ("trading-bot", "pin python dependencies in requirements", "5fde76c1", {"9dd0b4e9", "1864a4ba", "5cd538a9", "771d9de3"}, False),
            ("storefront", "deploy latest release to production on vercel", "5cd538a9", {"771d9de3", "34230a0d", "5fde76c1"}, False),
            ("agency-site", "redesign the hero section cards", "ed5bd42c", {"9dd0b4e9", "34230a0d", "5fde76c1"}, True),
            ("storefront", "commit new checkout page copy", "32559e0b", {"34230a0d", "5fde76c1", "7cc562d0"}, False),
            ("trading-bot", "commit docker container fix to main", "2067e6c2", {"9dd0b4e9", "760fc36e", "1864a4ba"}, False),
            ("agency-site", "write client landing page copy", "760fc36e", {"9dd0b4e9", "34230a0d", "5fde76c1", "32559e0b"}, False),
        ]
        for project, prompt, must_include, must_exclude, expect_conflict in cases:
            hits = g.recall(project, [], prompt, log=False)
            hit_ids = {h.id for h in hits}
            assert must_include in hit_ids, f"{project}/{prompt} missing {must_include}"
            assert not (hit_ids & must_exclude), f"{project}/{prompt} leaked {hit_ids & must_exclude}"
            if expect_conflict:
                assert any(h.conflicts for h in hits), f"{project}/{prompt} did not flag open conflict"
    finally:
        g.close()


def test_retired_lesson_excluded_from_recall_and_active_list(g):
    lid = g.learn("use legacy webpack 4 config", scopes=[("project", "shop")])["lesson"]
    assert lid in ids(g.recall("shop", [], "webpack config"))
    g.retire(lid)
    assert lid not in ids(g.recall("shop", [], "webpack config"))
    assert all(r["id"] != lid for r in g.lessons(include_inactive=False))
    assert any(r["id"] == lid and r["status"] == "retired" for r in g.lessons(include_inactive=True))


def test_supersede_clears_existing_contradicts_edge(g):
    a = g.learn("dark glass hero cards", scopes=[("project", "agency")])["lesson"]
    b = g.learn("editorial photography hero layout", scopes=[("project", "agency")])["lesson"]
    g.contradict(a, b)
    assert g.stats()["conflicts"] == 1
    g.supersede(b, a)
    assert g.stats()["conflicts"] == 0
    hits = g.recall("agency", [], "hero")
    assert ids(hits) == [b]
    assert hits[0].conflicts == []
    assert hits[0].replaced == 1


def test_supersede_unknown_lesson_raises_key_error(g):
    a = g.learn("real rule", scopes=[("tool", "git")])["lesson"]
    with pytest.raises(KeyError):
        g.supersede(a, "deadbeef")


def test_invalid_scope_kind_raises_value_error(g):
    with pytest.raises(ValueError):
        g.learn("bad scope rule", scopes=[("workspace", "foo")])


def test_explicit_tool_param_matches_without_project_uses_edge(g):
    lid = g.learn("multi-stage builds in docker", scopes=[("tool", "docker")])["lesson"]
    assert lid not in ids(g.recall("fresh-repo", [], "build container"))
    assert lid in ids(g.recall("fresh-repo", ["docker"], "build container"))


def test_multi_scope_tool_and_task_requires_both(g):
    lid = g.learn("use imperative commit messages", scopes=[("tool", "git"), ("task", "commit")])["lesson"]
    g.note_project("shop", ["git"])
    assert lid not in ids(g.recall("shop", [], "refactor checkout styles"))
    assert lid in ids(g.recall("shop", [], "please COMMIT the checkout styles"))


def test_repeat_weight_caps_at_four_repeats(g):
    lid = g.learn("never force push main branch", scopes=[("tool", "git")])["lesson"]
    for i in range(6):
        r = g.learn(f"never force push main branch again {i}", scopes=[("tool", "git")])
        assert r["repeat"] and r["lesson"] == lid
    hits = g.recall("x", ["git"], "")
    hit = next(h for h in hits if h.id == lid)
    # tool (2.0) + capped repeats (4 * 0.5 = 2.0) = 4.0
    assert hit.repeats == 6
    assert hit.score == 4.0


def test_render_context_includes_repeat_and_conflict_guard(g):
    from toldonce.cli import render_context
    a = g.learn("use tabs for indentation", scopes=[("project", "p")])["lesson"]
    g.learn("use tabs for indentation please", scopes=[("project", "p")])
    b = g.learn("use two spaces for indentation", scopes=[("project", "p")])["lesson"]
    g.contradict(a, b)
    block = render_context(g.recall("p", [], "format code"))
    assert "<toldonce>" in block and "</toldonce>" in block
    assert "(told 2 times)" in block
    assert "ask before choosing" in block


def test_expert_cypher_console_queries_on_demo_store(tmp_path):
    """Verify all 5 Interactive Cypher Console expert queries execute cleanly on demo/lessons.rl."""
    import shutil
    demo_db = Path(__file__).parent.parent / "demo" / "lessons.rl"
    copy_db = tmp_path / "demo_cy.rl"
    shutil.copy2(demo_db, copy_db)
    g = LessonGraph(str(copy_db))
    try:
        g.recall("storefront", [], "commit checkout copy", log=True)
        sup_rows = g.q("""MATCH path = (newer:Lesson)-[:SUPERSEDES*1..]->(older:Lesson)
            RETURN newer.id AS headId, newer.text AS activeRule, older.id AS replacedId,
                   older.text AS replacedRule, length(path) AS depth ORDER BY depth DESC, headId""")
        assert len(sup_rows) == 1 and sup_rows[0]["headId"] == "5cd538a9" and sup_rows[0]["replacedId"] == "771d9de3"

        con_rows = g.q("""MATCH (a:Lesson)-[:CONTRADICTS]-(b:Lesson)
            WHERE a.id < b.id AND a.status = 'active' AND b.status = 'active'
              AND NOT EXISTS { (:Lesson)-[:SUPERSEDES]->(a) } AND NOT EXISTS { (:Lesson)-[:SUPERSEDES]->(b) }
            RETURN a.id AS lessonA, b.id AS lessonB""")
        assert len(con_rows) == 1 and {con_rows[0]["lessonA"], con_rows[0]["lessonB"]} == {"7cc562d0", "ed5bd42c"}

        rep_rows = g.q("""MATCH (c:Correction)-[:REPEATS]->(l:Lesson)
            RETURN l.id AS lessonId, count(c) + 1 AS timesTold ORDER BY timesTold DESC, lessonId""")
        assert len(rep_rows) == 3 and all(r["timesTold"] >= 2 for r in rep_rows)

        mat_rows = g.q("""MATCH (p:Project)-[:USES]->(t:Tool)<-[:APPLIES_TO]-(l:Lesson)
            WHERE l.status = 'active' AND NOT EXISTS { (:Lesson)-[:SUPERSEDES]->(l) }
            RETURN p.name AS project, t.name AS tool, count(l) AS activeRules, collect(l.id) AS lessonIds
            ORDER BY project, activeRules DESC, tool""")
        assert len(mat_rows) >= 6
        assert any(r["project"] == "storefront" and r["tool"] == "git" and r["activeRules"] == 3 for r in mat_rows)

        prov_rows = g.q("""MATCH (r:Recall)-[s:SURFACED]->(l:Lesson)
            RETURN r.project AS project, s.rank AS rank, l.id AS lessonId ORDER BY s.rank ASC""")
        assert len(prov_rows) >= 1 and prov_rows[0]["project"] == "storefront"
    finally:
        g.close()


def test_scoped_pack_exporter_zero_leak_and_conflict_quarantine(tmp_path):
    """Verify .claude/rules/ and AGENTS.md export: zero cross-project leaks, zero stale rules, conflict quarantine."""
    import shutil
    demo_db = Path(__file__).parent.parent / "demo" / "lessons.rl"
    copy_db = tmp_path / "demo_exp.rl"
    shutil.copy2(demo_db, copy_db)
    g = LessonGraph(str(copy_db))
    try:
        sf = g.export_scoped_pack("storefront", fmt="claude", exclude_conflicts=True)
        assert sf["filename"] == ".claude/rules/toldonce-storefront.md"
        assert "9dd0b4e9" in sf["active_ids"]          # storefront pnpm rule included
        assert "5cd538a9" in sf["active_ids"]          # vercel CI rule inherited via :USES
        assert "771d9de3" not in sf["active_ids"]      # superseded vercel --prod excluded
        assert "34230a0d" not in sf["active_ids"]      # trading-bot rule never leaks
        assert "5fde76c1" not in sf["active_ids"]      # python rule never leaks into storefront
        assert "7cc562d0" not in sf["active_ids"]      # agency-site rule never leaks
        assert sf["quarantined_count"] == 0
        assert sf["other_project_excluded"] >= 3
        assert sf["superseded_excluded"] == 1
        assert "<!-- toldonce:" in sf["content"]
        assert "<!-- toldonce-audit:" in sf["content"]

        tb = g.export_scoped_pack("trading-bot", fmt="agents", exclude_conflicts=True)
        assert tb["filename"] == "AGENTS.md"
        assert "34230a0d" in tb["active_ids"]          # trading-bot strategy rule included
        assert "5fde76c1" in tb["active_ids"]          # python rule inherited via :USES
        assert "9dd0b4e9" not in tb["active_ids"]      # storefront pnpm rule never leaks
        assert "5cd538a9" not in tb["active_ids"]      # vercel rule never leaks into trading-bot

        ag = g.export_scoped_pack("agency-site", fmt="claude", exclude_conflicts=True)
        assert ag["quarantined_count"] == 2
        assert set(ag["quarantined_ids"]) == {"7cc562d0", "ed5bd42c"}
        assert "7cc562d0" not in ag["active_ids"] and "ed5bd42c" not in ag["active_ids"]
        assert "Open Conflicts (Quarantined" in ag["content"]
        assert "9dd0b4e9" not in ag["active_ids"]
    finally:
        g.close()


def test_cli_export_rules_and_mcp_export_rules_roundtrip(tmp_path):
    import shutil
    demo_db = Path(__file__).parent.parent / "demo" / "lessons.rl"
    copy_db = tmp_path / "demo_cli.rl"
    shutil.copy2(demo_db, copy_db)

    cli_out = subprocess.run(
        [sys.executable, "-m", "toldonce", "--db", str(copy_db), "export-rules", "--project", "storefront", "--format", "agents", "--json"],
        capture_output=True, text=True, check=True,
    ).stdout
    pack = json.loads(cli_out)
    assert pack["project"] == "storefront" and pack["format"] == "agents"
    assert "9dd0b4e9" in pack["active_ids"] and "34230a0d" not in pack["active_ids"]

    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "export_rules", "arguments": {"project": "agency-site", "format": "claude"}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "history", "arguments": {"id": "5cd538a9"}}},
    ]
    env = {**__import__("os").environ, "TOLDONCE_DB": str(copy_db)}
    mcp_out = subprocess.run(
        [sys.executable, "-c", "import toldonce.mcp as m, os; m.main(os.environ['TOLDONCE_DB'])"],
        input="\n".join(json.dumps(m) for m in msgs), capture_output=True, text=True, env=env, check=True,
    ).stdout.splitlines()
    replies = [json.loads(line) for line in mcp_out]
    exp_data = json.loads(replies[1]["result"]["content"][0]["text"])
    assert exp_data["quarantined_count"] == 2 and "9dd0b4e9" not in exp_data["active_ids"]
    hist_data = json.loads(replies[2]["result"]["content"][0]["text"])
    assert [c["id"] for c in hist_data["chain"]] == ["5cd538a9", "771d9de3"]


def test_detect_tools_fingerprints_repo_markers(tmp_path):
    from toldonce.store import detect_tools
    (tmp_path / ".git").mkdir()
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "pnpm-lock.yaml").write_text("", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    (tmp_path / "Dockerfile").write_text("FROM python:3.12", encoding="utf-8")
    (tmp_path / "vercel.json").write_text("{}", encoding="utf-8")
    found = detect_tools(tmp_path)
    assert found == ["docker", "git", "node", "pnpm", "python", "vercel"]



