# {{title}}

A Captain team view. Captain loads `captain/views/{{name}}/` from this project, lists it under the Explorer's Views,
and reloads it when a file changes. `captain-app check captain/views/{{name}}` loads it once with the Captain kit and
says exactly what's wrong. The view API (what the kit offers, which routes a view may call, and how the Explorer's
pick applies) is in Captain's backend/docs/extending.md, "Team views".
