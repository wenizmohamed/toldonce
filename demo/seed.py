"""Seed demo/lessons.rl with three weeks of a developer correcting a coding agent.

Every lesson below comes from a plain-language correction. Some are repeats
(the agent had to be told twice), one supersedes an older rule, and two
conflict. Run: python demo/seed.py
"""
import sys
from pathlib import Path

from toldonce import LessonGraph

OUT = Path(__file__).with_name("lessons.rl")

PROJECTS = {
    "storefront": ["git", "node", "pnpm", "nextjs", "tailwind", "vercel"],
    "trading-bot": ["git", "python", "docker"],
    "agency-site": ["git", "node", "vercel"],
}

# (said, lesson or None, scopes)
CORRECTIONS = [
    ("no, use pnpm in this repo, never npm", "Use pnpm, never npm", [("project", "storefront")]),
    ("always run the tests before you commit", "Run the test suite before every commit", [("tool", "git")]),
    ("don't use em dashes in client copy", "No em dashes in client-facing copy; use hyphens", [("task", "copy")]),
    ("never touch the live strategy params without asking", None, [("project", "trading-bot")]),
    ("stop adding new deps for one-liners", "Don't add a dependency for something a few lines can do", []),
    ("deploy with vercel --prod from the CLI", "Deploy with `vercel --prod`", [("tool", "vercel")]),
    ("no em dashes in the copy again, hyphens!", None, [("task", "copy")]),
    ("use the dark neon glass card style", "Hero sections use the dark glass card style", [("project", "agency-site")]),
    ("i said pnpm, not npm", None, [("project", "storefront")]),
    ("commit messages in imperative mood, under 72 chars", None, [("tool", "git"), ("task", "commit")]),
    ("pin python deps with exact versions", None, [("tool", "python")]),
    ("run the test suite before you commit, please", None, [("tool", "git")]),
    ("tailwind: no arbitrary values, use the theme tokens", None, [("tool", "tailwind")]),
    ("never force-push to main", None, [("tool", "git")]),
]


def main():
    if OUT.exists():
        print(f"{OUT} exists; delete it to reseed", file=sys.stderr)
        return 1
    g = LessonGraph(str(OUT))
    for name, tools in PROJECTS.items():
        g.note_project(name, tools)
    ids = {}
    for said, lesson, scopes in CORRECTIONS:
        r = g.learn(said, lesson, scopes, session="demo")
        ids.setdefault((lesson or said), r["lesson"])
    # The process changed: deploys now go through CI, so the old rule is replaced, not deleted.
    old = ids["Deploy with `vercel --prod`"]
    g.learn("stop deploying by hand, merges to main deploy through the GitHub action",
            "Never deploy by hand; merging to main deploys via the GitHub action",
            [("tool", "vercel")], session="demo", supersedes=old)
    # A later instruction conflicts with an earlier one; ToldOnce flags it instead of guessing.
    glass = ids["Hero sections use the dark glass card style"]
    clean = g.learn("no more dark neon glass, it looks AI-built; editorial layout with real photos",
                    "Hero sections use an editorial layout with real photography, not dark glass cards",
                    [("project", "agency-site")], session="demo")["lesson"]
    g.contradict(clean, glass)
    st = g.stats()
    print(f"seeded {OUT.name}: {st['lessons']} lessons, {st['corrections']} corrections, "
          f"{st['repeats']} repeats, {st['superseded']} superseded, {st['conflicts']} conflict")
    g.close()


if __name__ == "__main__":
    sys.exit(main())
