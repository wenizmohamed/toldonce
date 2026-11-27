import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import init, { RelateDb } from "../web/vendor/relate.js";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const WASM_BYTES = fs.readFileSync(path.join(ROOT, "web", "vendor", "relate_bg.wasm"));
const SEED_SQL = fs.readFileSync(path.join(ROOT, "web", "seed.cypher"), "utf8");
const RECALL_CYPHER = fs.readFileSync(path.join(ROOT, "web", "recall.cypher"), "utf8");
const PY_RECALL_CYPHER = fs.readFileSync(path.join(ROOT, "toldonce", "recall.cypher"), "utf8");

let wasmReady = false;
async function freshDb() {
  if (!wasmReady) {
    await init({ module_or_path: WASM_BYTES });
    wasmReady = true;
  }
  const db = new RelateDb();
  for (const raw of db.executeScript(SEED_SQL, null)) {
    const env = JSON.parse(raw);
    if (env.error) throw new Error(JSON.stringify(env.error));
  }
  return db;
}

function run(db, query, params = null) {
  const env = JSON.parse(db.execute(query, params ? JSON.stringify(params) : null));
  if (env.error) throw new Error(typeof env.error === "string" ? env.error : JSON.stringify(env.error));
  return env.rows;
}

test("web/recall.cypher is byte-for-byte identical to toldonce/recall.cypher", () => {
  const webBuf = fs.readFileSync(path.join(ROOT, "web", "recall.cypher"));
  const pyBuf = fs.readFileSync(path.join(ROOT, "toldonce", "recall.cypher"));
  assert.equal(Buffer.compare(webBuf, pyBuf), 0);
  assert.equal(RECALL_CYPHER, PY_RECALL_CYPHER);
});

test("WASM engine loads seed.cypher with expected graph topology", async () => {
  const db = await freshDb();
  const [counts] = run(db, `RETURN COUNT { (:Project) } AS projects, COUNT { (:Tool) } AS tools,
    COUNT { (:Task) } AS tasks, COUNT { (:Lesson) } AS lessons,
    COUNT { (:Correction) } AS corrections, COUNT { (:Correction)-[:REPEATS]->() } AS repeats,
    COUNT { ()-[:SUPERSEDES]->() } AS supersedes, COUNT { ()-[:CONTRADICTS]->() } AS conflicts`);
  assert.equal(counts.projects, 3);
  assert.equal(counts.lessons, 13);
  assert.equal(counts.repeats, 3);
  assert.equal(counts.supersedes, 1);
  assert.equal(counts.conflicts, 1);
});

test("WASM recall.cypher enforces zero cross-project leaks across all 3 projects", async () => {
  const db = await freshDb();
  const sf = run(db, RECALL_CYPHER, { project: "storefront", tools: [], text: "install dependencies and commit copy" }).map(r => r.id);
  const tb = run(db, RECALL_CYPHER, { project: "trading-bot", tools: [], text: "install deps and update strategy" }).map(r => r.id);
  const ag = run(db, RECALL_CYPHER, { project: "agency-site", tools: [], text: "redesign hero section" }).map(r => r.id);

  assert.ok(sf.includes("9dd0b4e9"));
  assert.ok(!sf.includes("34230a0d") && !sf.includes("5fde76c1") && !sf.includes("7cc562d0"));

  assert.ok(tb.includes("34230a0d") && tb.includes("5fde76c1"));
  assert.ok(!tb.includes("9dd0b4e9") && !tb.includes("1864a4ba") && !tb.includes("5cd538a9"));

  assert.ok(ag.includes("7cc562d0") && ag.includes("ed5bd42c"));
  assert.ok(!ag.includes("9dd0b4e9") && !ag.includes("34230a0d"));
});

test("WASM recall.cypher suppresses superseded lesson 771d9de3 and surfaces replacement 5cd538a9", async () => {
  const db = await freshDb();
  const rows = run(db, RECALL_CYPHER, { project: "storefront", tools: [], text: "deploy on vercel" });
  const ids = rows.map(r => r.id);
  assert.ok(ids.includes("5cd538a9"));
  assert.ok(!ids.includes("771d9de3"));
  const head = rows.find(r => r.id === "5cd538a9");
  assert.equal(head.replaced, 1);
});

test("WASM recall.cypher surfaces symmetric CONTRADICTS conflict guard on agency-site", async () => {
  const db = await freshDb();
  const rows = run(db, RECALL_CYPHER, { project: "agency-site", tools: [], text: "redesign hero" });
  const glass = rows.find(r => r.id === "7cc562d0");
  const edit = rows.find(r => r.id === "ed5bd42c");
  assert.deepEqual(glass.conflicts, ["ed5bd42c"]);
  assert.deepEqual(edit.conflicts, ["7cc562d0"]);
});

test("WASM Interactive Cypher Console: all 5 expert Cypher queries succeed with expected rows", async () => {
  const db = await freshDb();
  // Seed one Recall provenance entry just like the browser demo does on load
  run(db, "CREATE (:Recall {id: 'rc000001', at: datetime(), project: 'storefront', prompt: 'commit checkout copy'})");
  run(db, "MATCH (r:Recall {id: 'rc000001'}), (l:Lesson {id: '9dd0b4e9'}) CREATE (r)-[:SURFACED {rank: 1, score: 5.5}]->(l)");

  const supersedes = run(db, `MATCH path = (newer:Lesson)-[:SUPERSEDES*1..]->(older:Lesson)
    RETURN newer.id AS headId, newer.text AS activeRule, older.id AS replacedId, older.text AS replacedRule,
           length(path) AS depth, [(newer)-[:APPLIES_TO]->(s) | toLower(labels(s)[0]) + ':' + s.name] AS scopes
    ORDER BY depth DESC, headId`);
  assert.equal(supersedes.length, 1);
  assert.equal(supersedes[0].headId, "5cd538a9");
  assert.equal(supersedes[0].depth, 1);

  const conflicts = run(db, `MATCH (a:Lesson)-[:CONTRADICTS]-(b:Lesson)
    WHERE a.id < b.id AND a.status = 'active' AND b.status = 'active'
      AND NOT EXISTS { (:Lesson)-[:SUPERSEDES]->(a) } AND NOT EXISTS { (:Lesson)-[:SUPERSEDES]->(b) }
    RETURN a.id AS lessonA, a.text AS ruleA, b.id AS lessonB, b.text AS ruleB`);
  assert.equal(conflicts.length, 1);
  assert.deepEqual([conflicts[0].lessonA, conflicts[0].lessonB].sort(), ["7cc562d0", "ed5bd42c"]);

  const repeats = run(db, `MATCH (c:Correction)-[:REPEATS]->(l:Lesson)
    RETURN l.id AS lessonId, l.text AS lesson, count(c) + 1 AS timesTold
    ORDER BY timesTold DESC, lessonId`);
  assert.equal(repeats.length, 3);
  assert.ok(repeats.every(r => r.timesTold === 2));

  const matrix = run(db, `MATCH (p:Project)-[:USES]->(t:Tool)<-[:APPLIES_TO]-(l:Lesson)
    WHERE l.status = 'active' AND NOT EXISTS { (:Lesson)-[:SUPERSEDES]->(l) }
    RETURN p.name AS project, t.name AS tool, count(l) AS activeRules, collect(l.id) AS lessonIds
    ORDER BY project, activeRules DESC, tool`);
  assert.ok(matrix.length >= 6);
  const sfGit = matrix.find(r => r.project === "storefront" && r.tool === "git");
  assert.equal(sfGit.activeRules, 3);

  const provenance = run(db, `MATCH (r:Recall)-[s:SURFACED]->(l:Lesson)
    RETURN r.project AS project, s.rank AS rank, l.id AS lessonId ORDER BY s.rank ASC`);
  assert.equal(provenance.length, 1);
  assert.equal(provenance[0].lessonId, "9dd0b4e9");
});

test("WASM custom Cypher error returns structured error envelope without crashing engine", async () => {
  const db = await freshDb();
  const bad = JSON.parse(db.execute("MATCH (n RETURN n", null));
  assert.ok(bad.error, "invalid Cypher should return an error field");
  const ok = run(db, "RETURN 1 + 1 AS two");
  assert.equal(ok[0].two, 2);
});

test("WASM Scoped Export Cypher isolates projects, excludes superseded, and flags agency-site conflicts", async () => {
  const db = await freshDb();
  const q = `MATCH (l:Lesson)
    WHERE l.status = 'active' AND NOT EXISTS { (:Lesson)-[:SUPERSEDES]->(l) }
    WITH l,
      EXISTS { (l)-[:APPLIES_TO]->(:Project {name: $project}) } AS hereProject,
      EXISTS { (l)-[:APPLIES_TO]->(p:Project) WHERE p.name <> $project } AS otherProject,
      [(l)-[:APPLIES_TO]->(t:Tool) WHERE EXISTS { (:Project {name: $project})-[:USES]->(t) } | t.name] AS tools,
      COUNT { (l)-[:APPLIES_TO]->(:Tool) } AS nTools,
      [(l)-[:CONTRADICTS]-(o:Lesson) WHERE o.status = 'active' AND NOT EXISTS { (:Lesson)-[:SUPERSEDES]->(o) } | o.id] AS conflicts
    WHERE (hereProject OR NOT otherProject) AND (nTools = 0 OR size(tools) > 0 OR hereProject)
    RETURN l.id AS id, hereProject, tools, conflicts`;

  const sf = run(db, q, { project: "storefront" });
  const sfIds = sf.map(r => r.id);
  assert.ok(sfIds.includes("9dd0b4e9") && sfIds.includes("5cd538a9"));
  assert.ok(!sfIds.includes("771d9de3") && !sfIds.includes("34230a0d") && !sfIds.includes("5fde76c1"));
  assert.equal(sf.filter(r => r.conflicts.length > 0).length, 0);

  const ag = run(db, q, { project: "agency-site" });
  const agQuarantined = ag.filter(r => r.conflicts.length > 0).map(r => r.id).sort();
  assert.deepEqual(agQuarantined, ["7cc562d0", "ed5bd42c"]);
});

test("WASM live transitive SUPERSEDES mutation hides prior version on immediate recall", async () => {
  const db = await freshDb();
  run(db, "CREATE (:Lesson {id: 'v3ci0001', text: 'Deploy via signed GitHub Actions release tags only', created: datetime(), status: 'active'})");
  run(db, "MATCH (n:Lesson {id: 'v3ci0001'}), (t:Tool {name: 'vercel'}) CREATE (n)-[:APPLIES_TO]->(t)");
  run(db, "MATCH (n:Lesson {id: 'v3ci0001'}), (o:Lesson {id: '5cd538a9'}) CREATE (n)-[:SUPERSEDES]->(o)");

  const rows = run(db, RECALL_CYPHER, { project: "storefront", tools: [], text: "deploy release" });
  const ids = rows.map(r => r.id);
  assert.ok(ids.includes("v3ci0001"));
  assert.ok(!ids.includes("5cd538a9"));
  assert.ok(!ids.includes("771d9de3"));
  assert.equal(rows.find(r => r.id === "v3ci0001").replaced, 2);
});

test("web/index.html contains all Rank #1 interactive controls and zero broken template tokens", () => {
  const html = fs.readFileSync(path.join(ROOT, "web", "index.html"), "utf8");
  for (const marker of [
    'id="cypherSection"',
    'id="cyTableWrap"',
    'id="cyViewSeg"',
    'data-cy="supersedes"',
    'data-cy="conflicts"',
    'data-cy="repeats"',
    'data-cy="matrix"',
    'data-cy="provenance"',
    'id="exportSection"',
    'id="exportProj"',
    'id="exportFmtSeg"',
    'id="exportCopyBtn"',
    'id="exportDownloadBtn"',
    'id="exportPreview"',
    "SCOPED_EXPORT_CYPHER",
    "buildScopedExportPack",
    'id="cyProfileWrap"',
    'data-view="profile"',
    'data-fmt="cursor"',
    "resolveConflictLive",
    "renderProfileTree",
  ]) {
    assert.ok(html.includes(marker), `web/index.html missing required marker: ${marker}`);
  }
});

test("WASM native PROFILE returns relate.plan/v1 operator tree and phase telemetry", async () => {
  const db = await freshDb();
  const env = JSON.parse(db.execute("PROFILE " + RECALL_CYPHER, JSON.stringify({ project: "storefront", tools: [], text: "commit checkout copy" })));
  assert.equal(env.error, null);
  assert.ok(env.plan && env.plan.schema === "relate.plan/v1");
  assert.equal(env.plan.profiled, true);
  assert.ok(env.plan.root && typeof env.plan.root.operator === "string");
  assert.equal(typeof env.parse_ms, "number");
  assert.equal(typeof env.plan_ms, "number");
  assert.equal(typeof env.execute_ms, "number");
});

test("WASM live 1-click conflict resolution supersedes loser and clears CONTRADICTS edge", async () => {
  const db = await freshDb();
  run(
    db,
    "MATCH (n:Lesson {id: $n}), (o:Lesson {id: $o}) MERGE (n)-[:SUPERSEDES]->(o) WITH n, o OPTIONAL MATCH (n)-[c:CONTRADICTS]-(o) DELETE c RETURN count(*) AS k",
    { n: "ed5bd42c", o: "7cc562d0" }
  );

  const rows = run(db, RECALL_CYPHER, { project: "agency-site", tools: [], text: "redesign hero" });
  const ids = rows.map(r => r.id);
  assert.ok(ids.includes("ed5bd42c"));
  assert.ok(!ids.includes("7cc562d0"));
  const winner = rows.find(r => r.id === "ed5bd42c");
  assert.deepEqual(winner.conflicts, []);
  assert.equal(winner.replaced, 1);
});

