import copy
import dataclasses
import subprocess
import sys
from pathlib import Path

import pytest

from diligence.config import load_config
from diligence.data_access import AccessContext, DataAccess
from diligence.ledger import Ledger

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session", autouse=True)
def dataroom():
    if not (ROOT / "data" / "dataroom" / "northwind" / "pitch_deck.md").exists():
        subprocess.run([sys.executable, str(ROOT / "scripts" / "make_dataroom.py")], check=True)


@pytest.fixture
def cfg(tmp_path):
    base = load_config()
    settings = copy.deepcopy(base.settings)
    settings["paths"]["db"] = str(tmp_path / "test.db")
    settings["paths"]["out"] = str(tmp_path / "out")
    settings["paths"]["records"] = str(tmp_path / "records")
    return dataclasses.replace(base, settings=settings)


@pytest.fixture
def ledger(cfg):
    return Ledger(cfg.path("db"))


@pytest.fixture
def access(cfg, ledger):
    return DataAccess(cfg, ledger)


def ctx(user="vp_alice", deal="crm-003"):
    return AccessContext(user=user, deal_id=deal, run_id="test")
