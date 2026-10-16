import logging

def test_bug(caplog):
    caplog.set_level(logging.INFO)
    logging.info("before clear")
    caplog.clear()
    assert caplog.get_records("call") == []
    logging.info("after clear")
    assert [record.message for record in caplog.get_records("call")] == ["after clear"]
