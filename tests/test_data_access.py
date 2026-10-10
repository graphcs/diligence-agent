import pytest

from diligence.data_access import AccessDenied

from .conftest import ctx

LONG = "northwind/market_study_2026.txt"


def test_alice_sees_northwind_room_but_not_portfolio_files(access):
    files = [f["file_id"] for f in access.list_files(ctx("vp_alice", "crm-003"))]
    assert "northwind/pitch_deck.md" in files
    assert not any(f.startswith("portco_x/") for f in files)


def test_bob_sees_no_files_and_is_denied_reads(access, ledger):
    assert access.list_files(ctx("analyst_bob")) == []
    with pytest.raises(AccessDenied, match="no access"):
        access.read_file(ctx("analyst_bob"), "northwind/pitch_deck.md")
    decisions = [r[8] for r in ledger.audit_rows("test")]
    assert any(d.startswith("denied") for d in decisions)


def test_information_barrier_blocks_portfolio_data_on_other_deals(access):
    with pytest.raises(AccessDenied, match="information barrier"):
        access.read_file(ctx("vp_alice", "crm-003"), "portco_x/board_deck_q3.md")
    # ...but the same user can read it on that portfolio company's own deal.
    out = access.read_file(ctx("vp_alice", "crm-004"), "portco_x/board_deck_q3.md")
    assert "CANARY-PX-7731" in out["text"]


def test_search_never_returns_barrier_content(access):
    hits = access.search_files(ctx("vp_alice", "crm-003"), "Ferrous win rate price cut")
    assert all(not h["file_id"].startswith("portco_x/") for h in hits)
    assert all("CANARY" not in h["snippet"] for h in hits)


def test_paged_read_reports_truncation_and_next_page(access):
    m = access.get_file_manifest(ctx(), LONG)
    assert m["pages_total"] == 180
    first = access.read_file(ctx(), LONG, 1, 50)
    cov = first["coverage"]
    assert cov["pages_read"] == list(range(1, 21))  # capped at 20 pages per read
    assert cov["truncated"] is True and cov["next_page"] == 21
    last = access.read_file(ctx(), LONG, 175, 180)
    assert "termination notice" in last["text"]
    assert last["coverage"]["next_page"] is None


def test_malformed_and_traversal_ids_are_denied(access):
    for bad in ["../config/users.yaml", "northwind/../portco_x/board_deck_q3.md", "/etc/passwd"]:
        with pytest.raises(AccessDenied):
            access.read_file(ctx(), bad)


def test_deal_records_require_deal_team(access):
    assert access.get_deal(ctx("analyst_bob", "crm-001"), "crm-001")["name"] == "Lattice"
    with pytest.raises(AccessDenied):
        access.get_deal(ctx("analyst_bob", "crm-004"), "crm-004")


def test_lookup_returns_both_lattices(access):
    names = {c["name"] for c in access.lookup_company(ctx(), "Lattice")}
    assert names == {"Lattice", "Lattice Semiconductor"}


def test_concurrent_tool_calls_share_the_ledger_safely(access):
    """MCP servers run tools on worker threads; this used to raise sqlite3.InterfaceError."""
    from concurrent.futures import ThreadPoolExecutor

    queries = ["founder", "AutoStore", "patent", "Pickwell", "Ocado", "Pellucid", "ARR", "team"] * 4
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda q: access.search_files(ctx(), q), queries))
    assert len(results) == len(queries)
