"""End-to-end: real collectors, LLM adapter, citation check, git branches and review PR lifecycle.

Only the network (respx) and the GitHub PR API (FakeAPI) are faked; git runs against a real bare origin.
"""
import json
from datetime import date

import httpx
import pytest
import respx

from conftest import commits_by_bot_only, git, rows, show
from usescout import collectors
from usescout.config import Config
from usescout.models import Candidate
from usescout.runner import run

D = date(2026, 3, 2)


class FakeAPI:
    def __init__(self):
        self.prs, self.created = {}, 0

    def pr(self, n):
        return self.prs[n]

    def create_pr(self, head, base, title, body, labels):
        self.created += 1
        self.prs[self.created] = {"state": "open", "merged": False, "body": body}
        return self.created

    def update_pr(self, n, title, body):
        self.prs[n]["body"] = body


def cand(i, snippet, kind="repo"):
    return Candidate(source="fake", kind=kind, ref_id=f"o/r{i}", url=f"https://github.com/o/r{i}",
                     title=f"o/r{i}", snippet=snippet, extra={"path": "x.py"},
                     where="abstract" if kind == "paper" else "x.py",
                     evidence_url=f"https://github.com/o/r{i}/blob/abc/x.py")


SURE = "import mypkg\nmypkg.go()"           # heuristics: uses 0.9 -> auto-accept
UNSURE = "mypkg appears in a comment"       # heuristics: unknown 0.3 -> dropped
PAPER = "We used mypkg, a Python library"  # heuristics: mentions 0.6 -> review


@pytest.fixture
def fake(repo, monkeypatch):
    """Run the real runner with a scripted collector."""
    work, _ = repo
    batch = {"items": []}
    monkeypatch.setattr("usescout.runner.registry", lambda: {"fake": lambda ctx: list(batch["items"])})
    cfg = Config(profile="python", name="mypkg", repo="me/mypkg", sources=["fake"])
    return work, batch, cfg


# --------------------------------------------------------------------------------------------
def _openalex(request: httpx.Request) -> httpx.Response:
    u = str(request.url)
    paper = lambda i, doi, title: {"id": f"https://openalex.org/W{i}", "doi": f"https://doi.org/{doi}",  # noqa: E731
                                   "display_name": title, "publication_date": "2025-05-01",
                                   "authorships": [{"author": {"display_name": "Ann"}}]}
    if "/works/doi:10.1/pkg" in u:
        return httpx.Response(200, json={"id": "https://openalex.org/W1"})
    if "/works/doi:10.3/b" in u:  # reference list without our DOI -> uses without citing
        return httpx.Response(200, json={"referenced_works": ["https://openalex.org/W77"]})
    if "/works/doi:" in u:
        return httpx.Response(200, json={"referenced_works": []})
    if "cites%3AW1" in u or "cites:W1" in u:
        return httpx.Response(200, json={"results": [paper(2, "10.2/a", "Cites us"), paper(1, "10.1/pkg", "The package paper itself"),
                                                   paper(5, "10.32614/CRAN.package.mypkg", "CRAN record of the package")],
                                         "meta": {"next_cursor": None}})
    if "fulltext.search" in u:
        return httpx.Response(200, json={"results": [paper(2, "10.2/a", "Cites us"), paper(3, "10.3/b", "Uses us quietly"),
                                                   paper(4, "10.4/c", "Behind a paywall")],
                                         "meta": {"next_cursor": None}})
    return httpx.Response(404)


FULLTEXT_XML = (
    "<article><body><sec><title>Introduction</title><p>" + "Networks matter in many fields of science and engineering. " * 12 + "</p></sec>"
    "<sec><title>Methods</title><p>We exported the graphs with the <italic>mypkg</italic> Python library "
    "and plotted them in Gephi.</p></sec></body>"
    "<back><ref-list><ref>Vega Yon. mypkg: a package. 2021.</ref></ref-list></back></article>")


def _europepmc(request: httpx.Request) -> httpx.Response:
    q = request.url.params.get("query", "")
    if q.startswith("DOI:"):   # lookup of the open-access copy of a paper
        for doi, pmc in (("10.3/b", "PMC123"), ("10.2/a", "PMC456")):
            if doi in q:
                return httpx.Response(200, json={"resultList": {"result": [{"pmcid": pmc, "isOpenAccess": "Y"}]}})
        return httpx.Response(200, json={"resultList": {"result": [{"isOpenAccess": "N"}]}})   # no open full text
    return httpx.Response(200, json={"nextCursorMark": "*", "resultList": {"result": [
        {"doi": "10.5/e", "title": "T", "abstractText": "We analysed data with <i>mypkg</i>, a Python package.",
         "authorString": "Bo", "firstPublicationDate": "2025-01-01", "source": "MED", "id": "1"}]}})


def _llm(request: httpx.Request) -> httpx.Response:
    body = request.content.decode()
    if "just a comment" in body:
        out = {"usage_class": "false_positive", "confidence": 0.9, "rationale": "unrelated word"}
    else:
        out = {"usage_class": "uses", "confidence": 0.92, "rationale": "methods section says they used it"}
    return httpx.Response(200, json={"content": [{"text": json.dumps(out)}]})


@respx.mock
def test_all_sources_pipeline_with_llm_and_bot_only_commits(repo, tmp_path):
    work, _ = repo
    item = lambda full, fork=False, text="import mypkg": {  # noqa: E731
        "path": "a.py", "html_url": f"https://github.com/{full}/blob/abc123/a.py",
        "text_matches": [{"fragment": f"import os\nimport sys\n{text}\nx = 1\ny = 2\nz = 3"}],
        "repository": {"full_name": full, "html_url": f"https://github.com/{full}", "fork": fork,
                       "owner": {"login": full.split("/")[0]}}}
    respx.get("https://api.github.com/search/code").mock(return_value=httpx.Response(200, json={"items": [
        item("a/one"), item("b/fork", fork=True), item("me/mypkg"),  # fork and own repo are excluded
        item("cran/mypkg"),                                         # CRAN's mirror of the package itself
        item("c/comment", text="# just a comment about mypkg")]}))
    respx.get("https://packages.ecosyste.ms/api/v1/registries/pypi.org/packages/mypkg/dependent_packages").mock(
        return_value=httpx.Response(200, json=[{"name": "dep1", "repository_url": "https://github.com/acme/dep1",
                                                "registry_url": "https://pypi.org/project/dep1"}]))
    respx.get("https://papers.ecosyste.ms/api/v1/projects/pypi/mypkg/mentions").mock(return_value=httpx.Response(
        200, json=[{"paper_url": "https://papers.ecosyste.ms/api/v1/papers/10.3%2Fb"}]))
    respx.route(host="api.openalex.org").mock(side_effect=_openalex)
    respx.get("https://www.ebi.ac.uk/europepmc/webservices/rest/search").mock(side_effect=_europepmc)
    for pmc in ("PMC123", "PMC456"):
        respx.get(f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmc}/fullTextXML").mock(
            return_value=httpx.Response(200, text=FULLTEXT_XML))
    respx.post("https://api.anthropic.com/v1/messages").mock(side_effect=_llm)

    cfg = Config(profile="python", name="mypkg", repo="me/mypkg", doi=["10.1/pkg"],
                 sources=["github", "ecosystems", "openalex", "europepmc"],
                 llm={"provider": "anthropic", "api_key": "k"})
    api = FakeAPI()
    res = run(cfg, work, token="bot-token", search_token="pat", api=api, today=D, scratch=tmp_path / "s")
    assert not res.warnings, res.warnings

    f = {r["url"]: r for r in rows(work, "usescout", "data/findings.jsonl")}
    assert f["https://github.com/a/one"]["usage_class"] == "uses"
    assert f["https://pypi.org/project/dep1"]["usage_class"] == "depends"
    assert "https://github.com/b/fork" not in f and "https://github.com/me/mypkg" not in f
    assert "https://github.com/cran/mypkg" not in f
    assert "https://github.com/c/comment" not in f                         # LLM said false positive -> dropped
    assert f["https://doi.org/10.2/a"]["cites_package"] == "yes"
    assert f["https://doi.org/10.3/b"]["cites_package"] == "no"            # same paper from 3 sources, one finding
    assert f["https://doi.org/10.5/e"]["rationale"].startswith("LLM:")     # ambiguous snippet judged by the LLM
    assert len([r for r in f.values() if r["url"] == "https://doi.org/10.3/b"]) == 1

    # every finding says where it was found and shows the match itself
    one = f["https://github.com/a/one"]
    assert one["where"] == "a.py" and one["evidence_url"] == "https://github.com/a/one/blob/abc123/a.py"
    assert one["snippet"] == "import sys\nimport mypkg\nx = 1"            # the matching line +/- one line, not the head of the file
    b = f["https://doi.org/10.3/b"]                                       # no excerpt from the sources: fetched from the full text
    assert "Methods" in b["where"] and "mypkg" in b["snippet"] and b["evidence_url"].endswith("/PMC/PMC123")
    assert b["snippet"] == "We exported the graphs with the mypkg Python library and plotted them in Gephi."   # that paragraph only
    assert "abstract" in f["https://doi.org/10.5/e"]["where"] and "mypkg" in f["https://doi.org/10.5/e"]["snippet"]
    a = f["https://doi.org/10.2/a"]                                       # cites us AND we can show the sentence
    assert a["where"].startswith("its reference list cites 10.1/pkg (OpenAlex); excerpt from full text, section “Methods”")
    assert "mypkg" in a["snippet"]
    assert "https://doi.org/10.1/pkg" not in f                            # the package's own paper is not a user of it
    assert "https://doi.org/10.32614/cran.package.mypkg" not in f         # nor is its CRAN record
    assert "ecosyste.ms" in f["https://pypi.org/project/dep1"]["where"]
    assert res.counts["excerpts_fetched"] == 3                              # 10.2/a, 10.3/b (found) and 10.4/c (no open full text)
    assert "https://doi.org/10.4/c" not in f                                # weak signal, no excerpt: goes to review, not the database
    pend = {r["url"]: r for r in rows(work, "usescout-review", "data/review/pending.jsonl")}
    assert "no open-access full text found" in pend["https://doi.org/10.4/c"]["where"]
    assert pend["https://doi.org/10.4/c"]["snippet"] == ""
    assert "_No excerpt available" in show(work, "usescout-review", "data/review/REVIEW.md")
    papers_csv = show(work, "usescout", "data/papers.csv")
    assert "Methods" in papers_csv and "evidence_url" in papers_csv.splitlines()[0]
    assert "Methods" in show(work, "usescout", "REPORT.md") and "mypkg" in show(work, "usescout", "REPORT.md")
    assert "Uses us quietly" in show(work, "usescout", "REPORT.md")      # used without citing
    assert json.loads(show(work, "usescout", "badge.json"))["message"] == "2 repos · 3 papers"
    assert api.created == 1 and res.counts["llm_calls"] == 3   # the excerpt made 10.3/b judgeable

    assert commits_by_bot_only(work, "usescout") >= 1


# --------------------------------------------------------------------------------------------
def test_review_pr_lifecycle_merge_with_admin_edit(fake, tmp_path):
    work, batch, cfg = fake
    api = FakeAPI()
    kw = dict(api=api, today=D)
    batch["items"] = [cand(1, SURE), cand(2, UNSURE)] + [cand(10 + i, PAPER, "paper") for i in range(5)]
    r1 = run(cfg, work, scratch=tmp_path / "s1", **kw)
    assert (r1.counts["accepted"], r1.counts["to_review"], r1.counts["pr_opened"]) == (1, 5, 1)
    assert len(rows(work, "usescout", "data/findings.jsonl")) == 1
    assert len(rows(work, "usescout-review", "data/review/pending.jsonl")) == 5
    md = show(work, "usescout-review", "data/review/REVIEW.md")
    assert "**Found in:** abstract" in md and "> We used **mypkg**, a Python library" in md

    # PR still open: a new uncertain item is appended to the SAME PR
    batch["items"].append(cand(99, PAPER, "paper"))
    r2 = run(cfg, work, scratch=tmp_path / "s2", **kw)
    assert api.created == 1 and "pr_opened" not in r2.counts
    assert len(rows(work, "usescout-review", "data/review/pending.jsonl")) == 6

    # admin deletes one line on the review branch, then merges the PR
    git("checkout", "-q", "-B", "tmp-review", "origin/usescout-review", cwd=work)
    p = work / "data/review/pending.jsonl"
    lines = p.read_text().splitlines()
    removed = json.loads(lines[0])["id"]
    p.write_text("\n".join(lines[1:]) + "\n")
    git("add", "-A", cwd=work)
    git("commit", "-q", "-m", "admin edit", cwd=work)
    git("checkout", "-q", "-B", "tmp-data", "origin/usescout", cwd=work)
    git("merge", "-q", "--no-edit", "tmp-review", cwd=work)
    git("push", "-q", "origin", "HEAD:usescout", cwd=work)
    git("checkout", "-q", "main", cwd=work)
    api.prs[1].update(state="closed", merged=True)

    r3 = run(cfg, work, scratch=tmp_path / "s3", **kw)
    assert r3.counts["review_accepted"] == 5
    acc = {r["id"] for r in rows(work, "usescout", "data/findings.jsonl")}
    rej = {r["id"] for r in rows(work, "usescout", "data/rejected.jsonl")}
    assert removed in rej and removed not in acc and len(acc) == 6
    assert api.created == 1 and r3.counts["to_review"] == 0   # rejected items never come back
    badge = json.loads(show(work, "usescout", "badge.json"))
    assert badge["message"] == "1 repos · 5 papers"  # merged review items refresh the badge


def test_closed_pr_rejects_and_unchanged_run_makes_no_commit(fake, tmp_path):
    work, batch, cfg = fake
    api = FakeAPI()
    kw = dict(api=api, today=D)
    batch["items"] = [cand(1, SURE)] + [cand(10 + i, PAPER, "paper") for i in range(3)]
    run(cfg, work, scratch=tmp_path / "a", **kw)
    api.prs[1]["state"] = "closed"  # closed without merging
    run(cfg, work, scratch=tmp_path / "b", **kw)
    assert len(rows(work, "usescout", "data/rejected.jsonl")) == 3 and api.created == 1
    head = git("rev-parse", "origin/usescout", cwd=work)
    batch["items"][0].snippet = SURE + "\n# search returned the fragments in another order"   # same verdict
    run(cfg, work, scratch=tmp_path / "c", **kw)
    git("fetch", "-q", "origin", cwd=work)
    assert git("rev-parse", "origin/usescout", cwd=work) == head


def test_flood_is_capped_at_100_with_backlog_then_promoted(fake, tmp_path):
    work, batch, cfg = fake
    api = FakeAPI()
    batch["items"] = [cand(i, PAPER, "paper") for i in range(250)]
    r = run(cfg, work, scratch=tmp_path / "f", api=api, today=D)
    assert (r.counts["pr_pending"], r.counts["pr_backlog"]) == (100, 150)
    csv = show(work, "usescout-review", "data/review/backlog.csv").splitlines()
    assert len(csv) == 151 and csv[0].startswith("id,kind,title,url")
    assert commits_by_bot_only(work, "usescout-review") >= 1
    # merge the PR as-is; the next cycle opens a new PR with the next 100 from the backlog
    git("checkout", "-q", "-B", "tmp-data", "origin/usescout", cwd=work)
    git("merge", "-q", "--no-edit", "origin/usescout-review", cwd=work)
    git("push", "-q", "origin", "HEAD:usescout", cwd=work)
    git("checkout", "-q", "main", cwd=work)
    api.prs[1].update(state="closed", merged=True)
    r2 = run(cfg, work, scratch=tmp_path / "g", api=api, today=D)
    assert (r2.counts["pr_pending"], r2.counts["pr_backlog"]) == (100, 50) and api.created == 2


def test_spike_sends_everything_to_review_even_if_confident(fake, tmp_path):
    work, batch, cfg = fake
    api = FakeAPI()
    batch["items"] = [cand(1, SURE), cand(2, SURE)]
    run(cfg, work, scratch=tmp_path / "a", api=api, today=D)
    batch["items"] = [cand(100 + i, SURE) for i in range(200)]   # e.g. a name collision
    r = run(cfg, work, scratch=tmp_path / "b", api=api, today=D)
    assert r.counts["spike"] and r.counts["accepted"] == 0
    md = show(work, "usescout-review", "data/review/REVIEW.md")      # repos: file, link and the matching code in a fence
    assert "**Found in:** x.py ([open](https://github.com/o/r100/blob/abc/x.py))" in md
    assert "```\nimport mypkg\nmypkg.go()\n```" in md
    assert api.created == 1 and "name collision" in api.prs[1]["body"]


def test_data_in_main_branch_under_dot_usescout_with_pr_against_main(repo, tmp_path, monkeypatch):
    """data_branch == the checked-out branch: data goes to .usescout/ on main, history is kept, PR targets main,
    and a human pushing to main in the middle of the run does not break the bot's push."""
    work, origin = repo
    init = git("rev-parse", "HEAD", cwd=work)
    batch = [cand(1, SURE)] + [cand(10 + i, PAPER, "paper") for i in range(3)]

    def collect_while_human_pushes(ctx):
        other = tmp_path / "other"
        if not other.exists():
            git("clone", "-q", str(origin), str(other), cwd=tmp_path)
            (other / "human.txt").write_text("hi")
            git("add", "-A", cwd=other)
            git("-c", "user.name=Human", "-c", "user.email=h@example.org", "commit", "-q", "-m", "human work", cwd=other)
            git("push", "-q", "origin", "HEAD:main", cwd=other)
        return list(batch)
    monkeypatch.setattr("usescout.runner.registry", lambda: {"fake": collect_while_human_pushes})
    cfg = Config(profile="python", name="mypkg", repo="me/mypkg", sources=["fake"], data_branch="main")
    api = FakeAPI()
    r = run(cfg, work, api=api, today=D, scratch=tmp_path / "s")
    assert r.counts["accepted"] == 1 and api.created == 1

    assert len(rows(work, "main", ".usescout/data/findings.jsonl")) == 1
    assert json.loads(show(work, "main", ".usescout/badge.json"))["message"].startswith("1 repos")
    assert show(work, "main", "README.md") == "x" and show(work, "main", "human.txt") == "hi"   # nothing else touched
    assert len(rows(work, "usescout-review", ".usescout/data/review/pending.jsonl")) == 3
    log = git("log", "--format=%an", f"{init}..origin/main", cwd=work).splitlines()
    assert sorted(set(log)) == ["Human", "github-actions[bot]"] and log.count("Human") == 1


def test_a_failing_source_does_not_sink_the_run(fake, tmp_path, monkeypatch):
    work, _, cfg = fake

    def boom(ctx):
        raise RuntimeError("down")
    monkeypatch.setattr("usescout.runner.registry", lambda: {"fake": boom})
    res = run(cfg, work, scratch=tmp_path / "x", api=FakeAPI(), today=D)
    assert any("down" in w for w in res.warnings)


def test_registry_lists_real_sources():
    assert set(collectors.registry()) == {"github", "ecosystems", "openalex", "europepmc"}


def test_action_entrypoint_config_branch_wins_unless_input_is_set(repo, tmp_path, monkeypatch):
    """Regression: action.yml used to default `data-branch`, which silently overrode `data_branch` in the config."""
    import yaml

    from usescout import cli

    inputs = yaml.safe_load(open("action.yml"))["inputs"]
    assert inputs["data-branch"]["default"] == "" and inputs["llm-provider"]["default"] == ""   # defaults must not override

    work, origin = repo
    monkeypatch.setattr("usescout.runner.registry", lambda: {"fake": lambda ctx: [cand(1, SURE)]})
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))      # the entrypoint edits global git config
    env = {"GITHUB_WORKSPACE": str(work), "GITHUB_REPOSITORY": "me/mypkg", "INPUT_CONFIG": ".usescout.yml",
           "INPUT_DRY-RUN": "false", "INPUT_GITHUB-TOKEN": "t", "INPUT_SEARCH-TOKEN": "", "INPUT_LLM-PROVIDER": "",
           "INPUT_LLM-MODEL": "", "INPUT_LLM-API-KEY": "", "INPUT_OLLAMA-URL": "", "INPUT_DATA-BRANCH": ""}
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    def branches():
        return git("ls-remote", "--heads", "origin", cwd=work)

    (work / ".usescout.yml").write_text("profile: python\nname: mypkg\nsources: [fake]\ndata_branch: from-config\n")
    assert cli.main(["action"]) == 0
    assert "refs/heads/from-config" in branches()                              # the config file's branch is used

    monkeypatch.setenv("INPUT_DATA-BRANCH", "from-input")                      # an explicit input does win
    assert cli.main(["action"]) == 0
    assert "refs/heads/from-input" in branches()

    monkeypatch.setenv("INPUT_DATA-BRANCH", "")                                # old config name still works
    (work / ".usescout.yml").unlink()
    (work / ".whouses.yml").write_text("profile: python\nname: mypkg\nsources: [fake]\ndata_branch: legacy\n")
    assert cli.main(["action"]) == 0
    assert "refs/heads/legacy" in branches()
    assert len(rows(work, "legacy", "data/findings.jsonl")) == 1


@respx.mock
def test_github_rate_limit_on_a_later_query_keeps_earlier_results(repo, tmp_path):
    """Regression: a 429 on the second code-search query used to abort the whole source and drop the first query's repos."""
    from usescout.http import Http

    work, _ = repo

    def search(request: httpx.Request) -> httpx.Response:
        if request.url.params["q"].startswith('"import mypkg"'):
            return httpx.Response(200, json={"items": [{
                "path": "a.py", "html_url": "https://github.com/a/one/blob/abc/a.py",
                "text_matches": [{"fragment": "import mypkg"}],
                "repository": {"full_name": "a/one", "html_url": "https://github.com/a/one", "fork": False,
                               "owner": {"login": "a"}}}]})
        return httpx.Response(429, headers={"retry-after": "1"}, json={"message": "secondary rate limit"})
    route = respx.get("https://api.github.com/search/code").mock(side_effect=search)

    cfg = Config(profile="python", name="mypkg", repo="me/mypkg", sources=["github"])
    res = run(cfg, work, token="t", search_token="t", http=Http(sleep=lambda s: None), api=FakeAPI(),
              today=D, scratch=tmp_path / "s")
    assert [r["url"] for r in rows(work, "usescout", "data/findings.jsonl")] == ["https://github.com/a/one"]
    assert any("HTTP 429" in w for w in res.warnings) and res.counts["accepted"] == 1
    assert route.call_count >= 4        # the 429 was retried (with waits) before giving up on that query


@respx.mock
def test_a_long_retry_after_is_not_retried(repo, tmp_path):
    """GitHub said 'try again in 736s' (secondary rate limit): stop asking instead of retrying and extending the penalty."""
    from usescout.http import Http

    work, _ = repo
    route = respx.get("https://api.github.com/search/code").mock(return_value=httpx.Response(
        429, headers={"retry-after": "736", "x-ratelimit-remaining": "9", "x-ratelimit-resource": "code_search"},
        json={"message": "You have exceeded a secondary rate limit. try again in 736s"}))
    slept = []
    cfg = Config(profile="python", name="mypkg", repo="me/mypkg", sources=["github"])
    res = run(cfg, work, token="t", search_token="t", http=Http(sleep=slept.append), api=FakeAPI(),
              today=D, scratch=tmp_path / "s")
    assert route.call_count == 2                       # one request per query (python has two), no retries
    assert not any(s > 90 for s in slept)              # never sleeps minutes inside a job
    assert any("retry-after=736" in w and "code_search" in w for w in res.warnings)
