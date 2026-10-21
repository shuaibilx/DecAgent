from evals.integration.business_guest_browser_fixture import WORKBENCH_PATH


def test_guest_browser_fixture_points_to_workbench_route():
    assert WORKBENCH_PATH == "/"
