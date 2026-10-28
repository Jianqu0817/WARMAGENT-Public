"""Selected preserved-regression cases from MYATRI-AstrBot 4428069; fixtures only."""
import sqlite3

from warmagent.attention import followup

def test_window_is_one_to_four_speaker_local_and_expires():
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE attention_windows(scope TEXT PRIMARY KEY,remaining INTEGER,expires REAL,wake_key TEXT,sender_id TEXT)"
    )
    counts = set()
    for i in range(50):
        result = followup(db, "one", str(i), wake=True, now=0, sender_id='a')
        n = result["remaining"]
        counts.add(n)
        assert 1 <= n <= 4
        assert not followup(db, "other", "x", wake=False, now=1, sender_id='a')["active"]
        assert not followup(db, "one", "x", wake=False, now=1, sender_id='b')["active"]
        for j in range(n):
            assert followup(db, "one", str(j), wake=False, now=1, sender_id='a')["active"]
        assert not followup(db, "one", "end", wake=False, now=1, sender_id='a')["active"]
    assert counts == {1, 2, 3, 4}
    followup(db, "one", "wake", wake=True, now=0, sender_id='a')
    assert not followup(db, "one", "expired", wake=False, now=121, sender_id='a')["active"]
    db.close()
