def test_business_memory_reads_have_no_generic_jev_selector():
    from decagent.business.memory_write_evaluator import BusinessMemoryWriteEvaluator

    assert not hasattr(BusinessMemoryWriteEvaluator, "select")
