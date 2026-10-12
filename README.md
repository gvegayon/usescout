# usescout

Find out who uses your research software, even when they don't cite it.

`usescout` runs weekly as a GitHub Action in the repo of the package you maintain. It looks for evidence of use in package registries, GitHub code and the literature, classifies each hit (optionally with an LLM), checks whether the paper cites you, and keeps a database plus a "used by" badge on a data branch. Anything it is unsure about goes into **one review pull request** instead of the database.

## Quick start

1. Add `.usescout.yml` to your repo (see [`examples/`](examples)):

   ```yaml
   profile: r            # r | python | npm | cpp | julia | cli, or a list (lists such as queries and patterns are combined)
   name: rgexf
   repo: gvegayon/rgexf
   doi: [10.xxxx/your-package-doi]   # lets usescout detect citations
   ```
2. Add [`examples/workflow.yml`](examples/workflow.yml) as `.github/workflows/usescout.yml`.
3. Run it once with *workflow_dispatch*. Results appear on the `usescout` branch.
4. Badge: `https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/<owner>/<repo>/usescout/badge.json`

The action runs a prebuilt image from `ghcr.io/gvegayon/usescout` (the tag matching the action version), so nothing is built on your runner. Use `gvegayon/usescout@v0` to follow 0.x releases, or pin an exact version such as `@v0.2.3`. Locally: `docker run --rm -v $PWD:/github/workspace -e GITHUB_WORKSPACE=/github/workspace ghcr.io/gvegayon/usescout:latest dry-run --workspace /github/workspace` or `pipx install git+https://github.com/gvegayon/usescout`.

## Where the data lives

| `data_branch` | Where files go | Review PR targets |
|---|---|---|
| `usescout` (default) | root of that dedicated orphan branch | `usescout` |
| the branch your workflow checks out (e.g. `main`) | `.usescout/` folder on that branch (`data_dir` to change it) | `main` |

The default keeps weekly bot commits out of `main`, out of package tarballs (CRAN, PyPI, npm) and away from branch protection. Using `main` is simpler and puts everything in one place, but then: add `.usescout` to `.Rbuildignore`/`MANIFEST.in`/`.npmignore`, and allow `github-actions[bot]` to push to `main` (a protected `main` that requires PRs will reject the weekly update). If someone pushes to the branch mid-run, the bot rebases and retries.

Branches are `usescout` (data) and `usescout-review` (the review PR's branch). The review branch is deliberately flat: git cannot have both a branch `usescout` and a branch `usescout/review`. Change either with `data_branch` / `review_branch` in the config.

**Config file vs. action inputs.** The config file is the source of truth. An action input only overrides it when you set it explicitly in the workflow (`data-branch`, `llm-provider`, ...); leaving it out keeps what `.usescout.yml` says.

## Who authors what

Everything usescout writes is authored by **`github-actions[bot]`**: the database commits, the review-branch commits and the review PR. Commits carry no co-author trailers and are never signed with a personal key.

- Keep `github-token` at its default (`${{ github.token }}`); that is what makes the PR bot-authored. Do not pass a personal token there.
- GitHub code search sometimes rejects the default token. If so, add a PAT as `search-token`; it is used **only** to call the search API, never to commit or open PRs.
- Enable *Settings → Actions → General → "Allow GitHub Actions to create and approve pull requests"*, or the review PR cannot be opened. (PRs opened with the default token do not trigger other workflows, which is intended here.)

## Software profiles

A profile is YAML that says how use of a kind of software shows up: registry, code patterns, manifest files, code-search queries, literature terms. Built-ins live in [`src/usescout/profiles`](src/usescout/profiles); inspect one with `usescout profile show python --name my-pkg`. Put your own in `.usescout/profiles/<id>.yml` (use `extends:` to start from a built-in) or tweak a built-in with `overrides:` in `.usescout.yml`. Profiles are validated against [`schema.json`](src/usescout/profiles/schema.json).

## Sources

| Source | Status |
|---|---|
| `ecosystems`: dependents (packages.ecosyste.ms) and paper mentions (papers.ecosyste.ms) | implemented |
| `github`: code search | implemented; needs a token, and a PAT may be required |
| `openalex`: works citing your DOI + full-text search | implemented |
| `europepmc`: open-access full text | implemented |
| GitLab, arXiv PDFs, Semantic Scholar, social media | not yet |
| Bitbucket | no public cross-workspace code search API, so unsupported |

## Review flow

- confidence ≥ `review.accept` (0.85) → written to `usescout`.
- between `review.review` (0.4) and `accept` → added to a single PR from `usescout-review`. While it is open, new items are appended to it; no second PR is opened.
- Delete a line in `data/review/pending.jsonl` to reject an item, then merge to accept the rest. Rejected IDs are recorded in `data/rejected.jsonl`, so the same repository or paper is not proposed again. Closing the PR rejects everything in it (`review.on_close: snooze` re-proposes after `snooze_weeks`).
- The example workflow also runs when the default `usescout-review` PR is merged. That run moves the accepted records into `data/findings.jsonl` and refreshes `badge.json` immediately. If you customize `review_branch`, update the matching condition in `examples/workflow.yml`.
- Flood safeguard: at most `max_items_per_pr` (100) items are detailed in a PR. The rest go to `data/review/backlog.csv` (delete rows to bulk-reject) and are promoted in later PRs. If a run finds far more than usual (`spike_factor`), nothing is auto-accepted and the PR warns about a possible name collision.

## Evidence: where exactly was it found?

Every finding records where the package was found, so false positives are quick to spot (in the database, in `REPORT.md`, and in the review PR):

- `where`: the file in the repo, or `abstract` / `full text, section “Methods”` / `its reference list cites <doi>` for papers.
- `evidence_url`: a link to that file or article.
- `snippet`: a short excerpt that shows the match itself (at most 400 characters): the matching line of code plus one line of context, or the paragraph window around the term in the paper. In the review PR code is shown in a fenced block and prose is quoted with the term in bold.

Paper excerpts come from the open-access full text on Europe PMC (fetched in memory, never stored; up to `max_evidence_fetches` per run). Paywalled papers and preprints Europe PMC does not hold cannot be excerpted, and say so in `where`. Dependencies and citations are shown as such, without a text excerpt.

## LLMs

`llm-provider`: `anthropic`, `openai`, `gemini`, `ollama` or `none` (heuristics only). Only ambiguous hits are sent, capped by `llm.max_calls_per_run`, and answers are cached by evidence hash. Only the short excerpts described above are stored. Prompts treat snippets as data, but an LLM can still be fooled by hostile text, which is one reason uncertain items go to human review.

## Development

```
pip install -e '.[dev]'
pytest -q && ruff check .
```

## Renamed from `whouses`

usescout was called `whouses` until 0.2.0. If you used the old name: the repo URL redirects, `.whouses.yml` is still read when there is no `.usescout.yml` (with a warning), and the default branches changed from `whouses-data` / `whouses/review` to `usescout` / `usescout-review`. Existing data on the old branches is not migrated; set `data_branch: whouses-data` to keep using it, or let usescout regenerate it.
