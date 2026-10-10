from pathlib import Path

from diligence import guards, render
from diligence.models import Claim, EvidenceRecord, FileCoverage, MemoState, SectionDraft
from diligence.orchestrator import plan

FIX = Path(__file__).resolve().parent.parent / "data" / "fixtures" / "injection"


def test_injection_fixtures_are_flagged_and_benign_is_not():
    assert guards.looks_like_injection((FIX / "hidden_instruction.txt").read_text())
    assert guards.looks_like_injection((FIX / "image_exfil.txt").read_text())
    assert not guards.looks_like_injection((FIX / "benign.txt").read_text())


def test_strip_markup_removes_images_links_urls_comments():
    s = guards.strip_markup("Hi ![x](https://e.vil/?d=1) [site](https://a.b) https://c.d <!-- x --> end")
    assert s == "Hi site end"


def test_quote_matching_is_whitespace_tolerant_and_rejects_fabrication():
    src = ["ARR $4.8M (August 2026), up from\n$1.9M a year earlier."]
    assert guards.quote_in_source("ARR $4.8M (August 2026), up from $1.9M", src)
    assert guards.quote_in_source("ARR $4.8M ... a year earlier", src)
    assert not guards.quote_in_source("ARR $6.0M (August 2026)", src)


def test_plan_orders_dependencies_into_levels(cfg):
    levels = plan(cfg, "deep_dive", None)
    flat = [s for lvl in levels for s in lvl]
    assert flat.index("customers") < flat.index("financials")
    assert plan(cfg, "brief", ["competitors"]) == [["competitors"]]


def test_render_lists_coverage_unverified_and_access_gaps(ledger):
    ledger.add_evidence(EvidenceRecord(
        id="e1", run_id="r", deal_id="crm-003", section="product", claim="c", quote="q",
        source_type="doc", source_id="northwind/pitch_deck.md", source_uri="dataroom://x",
        title="Deck", as_of="2026-09-01", page=3, confidence="verified"))
    memo = MemoState(deal_id="crm-003", company="Northwind", stage="brief", requested_by="vp_alice",
                     access_gaps=["Data room exists but is not accessible to you."])
    memo.sections["product"] = SectionDraft(
        key="product", title="Product", claims=[
            Claim(text="Picks 600 units/hour", evidence_ids=["e1"]),
            Claim(text="Best in class", unverified=True)],
        coverage=[FileCoverage(file_id="f", title="Market study", pages_total=180,
                               pages_read=list(range(1, 21)))])
    md = render.to_markdown(ledger, memo)
    assert "Picks 600 units/hour [1]" in md
    assert "read 20/180 pages of \"Market study\"" in md
    assert "1 statement(s) had no recorded evidence" in md
    assert "not accessible to you" in md
    assert "AI-generated draft" in md
