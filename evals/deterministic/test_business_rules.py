"""Rule baseline uses actual sources and refuses ambiguous free-form plans."""

from decagent.business.contracts import PluginSpec
from decagent.business.tickets import builtin_manifest


def test_rule_baseline_parses_single_query_and_actual_entity_parameters():
    from decagent.business.rules import proposal

    spec = PluginSpec.from_mapping(builtin_manifest())
    evidence = {
        "directory": {
            "people": [
                {"id": "person-1", "name": "张伟", "team": "IT Support"},
                {"id": "person-2", "name": "张伟", "team": "Facilities"},
            ],
            "categories": [{"id": "category-1", "name": "Network"}],
        },
        "ticket": {"id": "ticket-1", "revision": 4, "status": "open"},
    }
    assert proposal(spec, "查看工单列表", evidence) == {"command": "list", "params": {}}
    assert proposal(spec, "请分配给 IT Support 的张伟", evidence) == {
        "command": "assign",
        "params": {"ticket_id": "ticket-1", "expected_revision": 4, "person_id": "person-1"},
    }
    assert proposal(spec, "请分配给张伟", evidence) is None
    assert proposal(spec, "不要关闭这个工单", evidence) is None
    assert proposal(spec, "请创建 Network 工单并分配给张伟", evidence) is None


def test_rule_baseline_creates_only_with_unique_real_category():
    from decagent.business.rules import proposal

    spec = PluginSpec.from_mapping(builtin_manifest())
    evidence = {"directory": {"categories": [{"id": "category-1", "name": "Network"}]}}
    instruction = "请创建 Network 工单，工作被阻断"
    result = proposal(spec, instruction, evidence)
    assert result["params"]["category_id"] == "category-1"
    assert (
        result["params"]["priority"] == "urgent" and result["params"]["description"] == instruction
    )
    assert proposal(spec, "请创建 Payroll 工单", evidence) is None
