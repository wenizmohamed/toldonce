# ToldOnce

**Tell your coding agent once.** Every correction you give a coding agent becomes a lesson in a [Relate](https://relatedb.ai) graph, linked to the projects, tools and tasks it covers. Before the agent acts, it recalls the lessons that connect to what you just asked.

Built for the Relate by Neo4j Hackathon ("Context, connected").

**Live demo (runs Relate in your browser via WebAssembly):** https://wenizmohamed.github.io/toldonce/web/

## The problem

You tell an agent "use pnpm in this repo, not npm". Two days later, a new session, it runs `npm install` again. Instruction files (`CLAUDE.md`, `.cursorrules`) help, but they are flat lists. They can't tell which rule applies to *this* request, they never notice you've said the same thing three times, and they quietly pile up rules that contradict each other.

Corrections aren't a list. They're connected context:

- "never force-push to main" is about **git**, so it applies to every project that uses git.
- "use pnpm" belongs to **one project**, and should never leak into another.
- "no em dashes in client copy" only matters when the **task** is writing copy.
- "deploy by hand with `vercel --prod`" was **replaced** by "merges deploy via CI", and the history is still useful.
- "dark glass hero cards" and "editorial layout, no glass" **conflict**. The agent should ask, not guess.

ToldOnce stores all of that as a graph and gets it back with one Cypher query.

## The graph

```
(:Correction {said, at, session})-[:TAUGHT]->(:Lesson {id, text, status})
(:Correction)-[:REPEATS]->(:Lesson)              # you had to say it again
(:Lesson)-[:APPLIES_TO]->(:Project|:Tool|:Task)
(:Project)-[:USES]->(:Tool)                      # fingerprinted from the repo
(:Lesson)-[:SUPERSEDES]->(:Lesson)               # newer rule wins; history kept
(:Lesson)-[:CONTRADICTS]->(:Lesson)              # surfaced, never silently resolved
(:Recall {project, prompt})-[:SURFACED {rank, score}]->(:Lesson)   # provenance
```

**Recall** ([`toldonce/recall.cypher`](toldonce/recall.cypher)) runs one statement over that graph:

- It drops lessons that are retired or superseded (`NOT EXISTS { (:Lesson)-[:SUPERSEDES]->(l) }`).
- It keeps lessons for this project, or global ones, and never another project's.
- It reaches tool lessons through `(:Project)-[:USES]->(:Tool)`, so a git lesson follows you into every git repo with no extra setup.
- It matches task lessons only when the task shows up in the prompt.
- It counts repeats and the length of the supersede chain, and lists conflicts.

Ranking blends those graph signals with Relate **full-text search** (`SEARCH l IN (FULLTEXT INDEX lesson_text FOR $q)`) over the lesson text. Every hit says *why* it surfaced, for example `tool git, task commit, text 0.81, told 2x`.

**Learning** checks full-text for an active lesson in an overlapping scope that already says the same thing. If one exists, the correction is linked as `REPEATS` instead of creating a duplicate. That gives you the metric this project is named after: **how often you had to repeat yourself.**

The browser demo runs the same `recall.cypher` file, and a test enforces that.

## Setup

Requires Python 3.10+. Relate comes in as a pip dependency (`relate-db`), so there's no server to run.

```bash
git clone https://github.com/wenizmohamed/toldonce && cd toldonce
python -m venv .venv && . .venv/bin/activate
pip install -e ".[test]"
pytest -q                      # 12 tests
```

### Try it on the demo store

`demo/lessons.rl` is a committed Relate store: three weeks of a developer correcting an agent across three projects. Regenerate it with `python demo/seed.py`.

```bash
export TOLDONCE_DB=demo/lessons.rl
toldonce list
toldonce recall "commit the new checkout page copy" --project storefront
toldonce recall "redesign the hero" --project agency-site      # shows the open conflict
toldonce show <lesson-id>                                      # supersede chain + every correction
toldonce stats                                                 # what you've had to say twice
```

### Teach it

```bash
toldonce learn "no, use pnpm here" --lesson "Use pnpm, never npm" --project storefront
toldonce learn "never force-push to main" --tool git
toldonce learn "no em dashes in client copy" --task copy
toldonce learn "merges deploy via CI now" --tool vercel --supersedes <old-id>
toldonce contradict <id-a> <id-b>
```

The store defaults to `~/.toldonce/lessons.rl`, one graph shared across all your repos. Override it with `--db` or `TOLDONCE_DB`.

### Wire it into your agent

**Claude Code:** merge [`integrations/claude-code/settings.json`](integrations/claude-code/settings.json) into `.claude/settings.json` (or `~/.claude/settings.json`):

- The `UserPromptSubmit` hook (`toldonce hook --capture`) fingerprints the repo (git, node, pnpm, python, docker, vercel…), records `(:Project)-[:USES]->(:Tool)`, and injects a `<toldonce>` block with the lessons that apply before the model sees your prompt.
- With `--capture`, prompts that read as corrections ("no, …", "don't …", "I told you …") are learned automatically, scoped to the current project.

**Any MCP host:** `toldonce-mcp` is a stdio MCP server with `recall`, `learn`, `supersede` and `history` tools. It is dependency-free and runs on the same store.

**Anything else:** `toldonce recall --cwd . --context "<prompt>"` prints the block to paste into a system prompt, and `--json` prints structured hits.

### Browser demo

`web/` is a static page. Relate's WebAssembly build (`@relate/relate-wasm`, vendored) loads `web/seed.cypher`, which is exported from the demo store with `toldonce export`. From there you can recall, record a correction (and watch repeat detection fire), view the lesson graph, and run any Cypher. To run it locally:

```bash
cd web && python -m http.server 8000   # open http://localhost:8000
```

## How it uses Relate

**Mode: single file.** The persistent store is [`demo/lessons.rl`](demo/lessons.rl) in this repo, and `~/.toldonce/lessons.rl` on a real machine. One `.rl` file per developer is the right shape for this project: the lesson graph follows you across every repo and agent session, you can copy it to a new machine or back it up as one file, and it stays durable through Relate's write-ahead log with no server to run. The browser demo loads the same graph into an in-memory WASM instance from `web/seed.cypher`, which is exported from that file.

| Capability | Where |
| --- | --- |
| Embedded Python library (`relate-db`), single-file `.rl` store with WAL | `toldonce/store.py`, `demo/lessons.rl` |
| Modern Cypher: `EXISTS {}` / `COUNT {}` subqueries, pattern comprehensions, variable-length `SUPERSEDES*1..` | `toldonce/recall.cypher` |
| Full-text index + `SEARCH` clause (hybrid with graph scoring) | recall + repeat detection |
| Uniqueness constraints | schema in `LessonGraph._schema` |
| WebAssembly build in the browser | `web/` |
| MCP (own stdio server over the embedded store) | `toldonce/mcp.py` |

## Tools and AI disclosure

- **Third-party:** Relate (`relate-db` 1.0.2, `@relate/relate-wasm` 1.0.2, Apache-2.0), Google Fonts on the demo page, and pytest. Nothing else.
- **AI use:** this project was built with significant help from Claude (Anthropic), working as a coding agent: design, code, tests, demo page and this README. The author directed the project and is responsible for it.
- **New work:** everything in this repository was written during the contest period (started November 17, 2026).

## Limits (honest list)

- Repeat detection is lexical (full-text plus token overlap), not semantic. A paraphrase with no shared words becomes a new lesson. Relate's vector index is the natural next step.
- `--capture` uses a conservative phrase list. It misses corrections that don't start like one, on purpose.
- One store per user is the intended shape. Relate is single-writer per process, which fits one developer and their agents.

## License

Apache-2.0
