// Shared by the Python package and the browser demo (web/). Params: $project, $tools, $text
MATCH (l:Lesson)
WHERE l.status = 'active' AND NOT EXISTS { (:Lesson)-[:SUPERSEDES]->(l) }
WITH l,
  EXISTS { (l)-[:APPLIES_TO]->(:Project {name: $project}) } AS hereProject,
  EXISTS { (l)-[:APPLIES_TO]->(p:Project) WHERE p.name <> $project } AS otherProject,
  [(l)-[:APPLIES_TO]->(t:Tool)
     WHERE t.name IN $tools OR EXISTS { (:Project {name: $project})-[:USES]->(t) } | t.name] AS tools,
  [(l)-[:APPLIES_TO]->(k:Task) WHERE $text CONTAINS toLower(k.name) | k.name] AS tasks,
  COUNT { (l)-[:APPLIES_TO]->() } AS nScopes,
  COUNT { (l)-[:APPLIES_TO]->(:Tool) } AS nTools,
  COUNT { (l)-[:APPLIES_TO]->(:Task) } AS nTasks,
  COUNT { (:Correction)-[:REPEATS]->(l) } AS repeats,
  COUNT { (l)-[:SUPERSEDES*1..]->(:Lesson) } AS replaced,
  [(l)-[:CONTRADICTS]-(o:Lesson) WHERE o.status = 'active' | o.id] AS conflicts,
  [(l)-[:APPLIES_TO]->(x) | toLower(labels(x)[0]) + ':' + x.name] AS scopes
WHERE (hereProject OR NOT otherProject)
  AND (nTools = 0 OR size(tools) > 0 OR hereProject)
  AND (nTasks = 0 OR size(tasks) > 0)
RETURN l.id AS id, l.text AS text, hereProject, tools, tasks, nScopes,
       repeats, replaced, conflicts, scopes
