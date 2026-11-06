import sqlite3, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from ledger import init_ledger, get_peer, upsert_peer, get_remote_repo, upsert_remote_repo, import_version, import_ticket, list_imported_tickets, get_latest_imported_manifest, list_imported, set_auto_approve, get_auto_approve, list_peers
from crypto import build_manifest, sign_manifest, manifest_hash
from nacl.signing import SigningKey

def memdb():
    c=sqlite3.connect(":memory:"); c.row_factory=sqlite3.Row; return init_ledger(c)

def _signed(sk, kid, repo="r1", version=1, prev=None):
    body=build_manifest(repo_id=repo, version=version, content_hash="ab"*32, size_bytes=10, signer_key_id=kid, submitter_key_id=kid, prev_manifest_hash=prev, state="accepted")
    return sign_manifest(sk, body)

def test_remote_tables_exist_and_queryable():
    c=memdb()
    upsert_peer(c,"pool-x","agent:alice","agent:maint",trust_state="verified")
    assert get_peer(c,"pool-x")["trust_state"]=="verified"
    upsert_remote_repo(c,"r1","pool-x",remote_repo_id="r1",link="https://example/r1")
    assert get_remote_repo(c,"r1")["link"]=="https://example/r1"
    sk=SigningKey.generate(); kid="agent:maint"
    m=_signed(sk,kid)
    import_version(c,m,manifest_hash(m))
    assert list_imported(c)[0]["repo_id"]=="r1"
    assert get_latest_imported_manifest(c,"r1")["version"]==1
    import_ticket(c,{"ticket_id":"t-1","pool_id":"pool-x","repo_id":"r1","title":"hi","state":"open"})
    assert list_imported_tickets(c,pool_id="pool-x")[0]["ticket_id"]=="t-1"
    set_auto_approve(c,"pool-x",False); assert get_auto_approve(c,"pool-x") is False
    assert len(list_peers(c))==1
