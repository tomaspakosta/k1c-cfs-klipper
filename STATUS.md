# k1c-cfs-klipper — stav

Klon na VPS `hub`: `/home/serverroot/projects/nosch/k1c-cfs-klipper`
Remote: `tomaspakosta/k1c-cfs-klipper`
Výchozí větev: `master`

## AI orchestrator (git)

- Lokální guard: `.git/hooks/pre-push` blokuje přímý push do `main`/`master`
- Agenti: `feature/<agent>/<task>` → Pull Request (`/opt/ai-orchestrator/scripts/`)
- Secrets / runtime data (`.env`, databáze, `venv/`) zůstávají mimo git dle `.gitignore`

## Poznámky

- Tento soubor je dokumentace; přidání nespouští redeploy aplikace.
