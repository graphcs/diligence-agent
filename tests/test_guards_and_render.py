from pathlib import Path

import io
import zipfile

from diligence import charts, guards, orchestrator, render, word
from diligence.agents.editor import clean_diagrams
from diligence.models import (Claim, Diagrams, EvidenceRecord, FileCoverage, FundingRound, MemoState,
                              ModelEdge, Quadrant, QuadrantPoint, RevenuePoint, RevenueTrend,
                              SectionDraft, SummaryPoint)
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


def _evidence(ledger, *ids):
    for i, e in enumerate(ids, 1):
        ledger.add_evidence(EvidenceRecord(
            id=e, run_id="r", deal_id="crm-003", section="s", claim="c", quote=f"q{i}",
            source_type="doc", source_id="d", source_uri=f"dataroom://{e}", title=f"Doc {e}",
            page=i, confidence="verified"))


def _memo_with_sections(*keys):
    memo = MemoState(deal_id="crm-003", company="Northwind", stage="deep_dive", requested_by="vp_alice")
    for k in keys:
        memo.sections[k] = SectionDraft(key=k, title=k.title(), claims=[Claim(text=f"{k} fact", evidence_ids=["e1"])])
    return memo


def test_gaps_section_sits_just_above_sources(ledger):
    _evidence(ledger, "e1")
    memo = _memo_with_sections("snapshot", "financials")
    memo.summary = [SummaryPoint(text="Point", evidence_ids=["e1"])]
    memo.consistency_issues = [SummaryPoint(text="Two ARR figures", evidence_ids=["e1"])]
    md = render.to_markdown(ledger, memo)
    heads = [line for line in md.splitlines() if line.startswith("## ")]
    assert heads == ["## Summary", "## Snapshot", "## Financials", "## Consistency checks",
                     "## Gaps and unverified", "## Sources"]


def _full_diagrams():
    return Diagrams(
        quadrant=Quadrant(x_low="Narrow", x_high="Broad", y_low="Low price", y_high="Premium", points=[
            QuadrantPoint(name="Northwind", x=0.3, y=0.8, evidence_ids=["e2"]),
            QuadrantPoint(name="Acme Robotics", x=0.7, y=0.4, evidence_ids=["e2"])]),
        revenue=RevenueTrend(metric="ARR", unit="$M", points=[
            RevenuePoint(period="FY2024", value=2.1, evidence_ids=["e3"]),
            RevenuePoint(period="FY2025", value=4.8, evidence_ids=["e3"])]),
        business_model=[ModelEdge(source="3PLs", target="Northwind", label="subscription", evidence_ids=["e1"]),
                        ModelEdge(source="Northwind", target="Integrators", label="rev share", evidence_ids=["e1"]),
                        ModelEdge(source="Integrators", target="3PLs", label="installs", evidence_ids=["e1"])],
        funding=[FundingRound(date="2023", round="Seed", amount="$3M", lead="", evidence_ids=["e1"]),
                 FundingRound(date="2024", round="Series A", amount="$12M", lead="Lux", evidence_ids=["e1"])])


def test_diagrams_are_embedded_as_images_inside_their_sections(ledger):
    _evidence(ledger, "e1", "e2", "e3")
    memo = _memo_with_sections("snapshot", "product", "competitors", "financials")
    memo.diagrams = _full_diagrams()
    md = render.to_markdown(ledger, memo)

    def section(title):
        start = md.index(f"## {title}")
        return md[start:md.index("\n## ", start + 1)]

    assert "![Funding history](diagrams/funding.png)" in section("Snapshot")
    assert "![Business model](diagrams/business_model.png)" in section("Product")
    assert "![Competitive positioning](diagrams/competitors.png)" in section("Competitors")
    assert "![Revenue trend](diagrams/revenue.png)" in section("Financials")
    assert "sources [2]" in section("Competitors") and "sources [3]" in section("Financials")


def test_charts_render_to_png_and_skip_thin_data():
    memo = _memo_with_sections("snapshot", "product", "competitors", "financials")
    memo.diagrams = _full_diagrams()
    pngs = charts.render_all(memo)
    assert sorted(pngs) == ["business_model.png", "competitors.png", "funding.png", "revenue.png"]
    assert all(p.startswith(b"\x89PNG") and len(p) > 5000 for p in pngs.values())
    memo.diagrams.quadrant.points = memo.diagrams.quadrant.points[:1]
    memo.diagrams.revenue.points = memo.diagrams.revenue.points[:1]
    del memo.sections["product"]
    assert sorted(charts.render_all(memo)) == ["funding.png"]


def test_save_memo_writes_charts_and_docx_next_to_memo(cfg):
    memo = _memo_with_sections("snapshot")
    memo.diagrams = _full_diagrams()
    out_md, _ = orchestrator.save_memo(cfg, memo, "vp_alice", run_id="r1")
    assert [p.name for p in (out_md.parent / "diagrams").glob("*.png")] == ["funding.png"]
    assert (out_md.parent / "memo.docx").read_bytes().startswith(b"PK")


def test_docx_embeds_charts_in_order_and_keeps_model_text_literal(ledger):
    _evidence(ledger, "e1", "e2", "e3")
    memo = _memo_with_sections("snapshot", "product", "competitors", "financials")
    memo.sections["product"].claims.append(Claim(text="Uses <b>magic</b> & \x07bell"))
    memo.diagrams = _full_diagrams()

    def parts(data):
        z = zipfile.ZipFile(io.BytesIO(data))
        return z.read("word/document.xml").decode(), [n for n in z.namelist() if n.startswith("word/media/")]

    xml, media = parts(word.to_docx(ledger, memo))
    assert len(media) == 4
    assert "Uses &lt;b&gt;magic&lt;/b&gt; &amp; bell" in xml
    assert xml.index("Gaps and unverified") < xml.index(">Sources<")
    memo.diagrams = Diagrams()
    assert parts(word.to_docx(ledger, memo))[1] == []


def test_diagram_below_threshold_is_not_drawn(ledger):
    _evidence(ledger, "e1")
    memo = _memo_with_sections("competitors", "financials")
    memo.diagrams.quadrant.points = [QuadrantPoint(name="Northwind", x=0.5, y=0.5, evidence_ids=["e1"])]
    memo.diagrams.revenue.points = [RevenuePoint(period="FY2025", value=4.8, evidence_ids=["e1"])]
    md = render.to_markdown(ledger, memo)
    assert "![" not in md


def test_clean_diagrams_keeps_only_cited_items():
    raw = {"quadrant": {"x_low": "A", "x_high": "B", "y_low": "C", "y_high": "D", "points": [
               {"name": "Northwind", "x": 1.7, "y": -2, "evidence_ids": ["e1"]},
               {"name": "Ghost", "x": 0.5, "y": 0.5, "evidence_ids": ["nope"]}]},
           "revenue": {"metric": "ARR", "unit": "$M", "points": [
               {"period": "FY2025", "value": 4.8, "evidence_ids": ["e1"]}]},
           "business_model": [{"source": "x", "target": "y", "label": "", "evidence_ids": []}],
           "funding": [{"date": "2024", "round": "Seed ![i](https://e.vil)", "amount": "", "lead": "",
                        "evidence_ids": ["e1"]}]}
    d = clean_diagrams(raw, {"e1"})
    assert [(p.name, p.x, p.y) for p in d.quadrant.points] == [("Northwind", 1.0, 0.0)]
    assert len(d.revenue.points) == 1 and d.business_model == []
    assert d.funding[0].round == "Seed"
    assert clean_diagrams(raw, {"e1"}, has_financials=False).revenue.points == []
    assert clean_diagrams({}, {"e1"}) == Diagrams()
