import sqlite3, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from ledger import init_ledger, create_pool, get_repo
from tickets import PoolService
from nacl.signing import SigningKey

def memdb():
    c=sqlite3.connect(":memory:"); c.row_factory=sqlite3.Row; return init_ledger(c)

def test_add_repo_link_registers_without_fetch(tmp_path):
    c=memdb(); create_pool(c,"pool-1","Team","agent:maint")
    svc=PoolService(c, caller_key_id="agent:maint", caller_signing_key=SigningKey.generate())
    out=svc.add_repo("pool-1","demo","",repo_link="https://example/demo.git")
    assert out["registered_link"] is True and out["fetched"] is False
    assert get_repo(c,"demo")["path"]=="https://example/demo.git"
    # No repo_versions created (no fetch/submit)
    assert c.execute("SELECT COUNT(*) FROM repo_versions WHERE repo_id='demo'").fetchone()[0]==0
