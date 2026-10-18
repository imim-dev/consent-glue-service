from glue.cli import main


def test_status_on_an_empty_database(tmp_path, capsys):
    assert main(["--db", str(tmp_path / "t.db"), "status"]) == 0
    out = capsys.readouterr().out
    assert "operations: none" in out and "dead letters: 0" in out


def test_dead_letters_and_replay_on_an_empty_database(tmp_path, capsys):
    db = str(tmp_path / "t.db")
    assert main(["--db", db, "dead-letters"]) == 0
    assert "no dead-lettered operations" in capsys.readouterr().out
    assert main(["--db", db, "replay"]) == 0
    assert "requeued 0" in capsys.readouterr().out
