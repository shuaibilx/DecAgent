from ops.deploy.sop_business_probe import assess_business_readiness


def _container(status="running", health="healthy", command=None, exit_code=0):
    return {
        "State": {
            "Status": status,
            "ExitCode": exit_code,
            "Health": {"Status": health} if health else None,
        },
        "Config": {"Cmd": command or []},
    }


def _ready_services():
    return {
        "postgres": _container(),
        "migrate": _container("exited", None, exit_code=0),
        "bootstrap": _container("exited", None, exit_code=0),
        "business-mcp": _container(),
        "api": _container(),
        "worker": _container(command=["worker", "--kind", "business"]),
    }


def test_business_probe_requires_api_dependencies_and_business_worker():
    report = assess_business_readiness(
        _ready_services(), {"status": "ok"}, {"status": "ready"}
    )

    assert report == {
        "status": "ready",
        "components": {
            "api_liveness": True,
            "api_readiness": True,
            "postgres": True,
            "mcp": True,
            "api_container": True,
            "business_worker": True,
            "migrations": True,
            "bootstrap": True,
        },
    }


def test_api_liveness_does_not_mask_unhealthy_business_worker():
    services = _ready_services()
    services["worker"]["State"]["Health"]["Status"] = "unhealthy"

    report = assess_business_readiness(
        services, {"status": "ok"}, {"status": "ready"}
    )

    assert report["components"]["api_liveness"] is True
    assert report["components"]["business_worker"] is False
    assert report["status"] == "not_ready"


def test_api_liveness_does_not_mask_mcp_or_migration_failure():
    services = _ready_services()
    services["business-mcp"]["State"]["Health"]["Status"] = "unhealthy"
    services["migrate"]["State"]["ExitCode"] = 1

    report = assess_business_readiness(
        services, {"status": "ok"}, {"status": "not_ready"}
    )

    assert report["components"]["api_liveness"] is True
    assert report["components"]["mcp"] is False
    assert report["components"]["migrations"] is False
    assert report["status"] == "not_ready"


def test_business_probe_rejects_missing_or_extra_services():
    services = _ready_services()
    del services["worker"]
    missing = assess_business_readiness(
        services, {"status": "ok"}, {"status": "ready"}
    )
    assert missing["status"] == "not_ready"
    assert missing["components"]["business_worker"] is False

    services = _ready_services()
    services["repository-repair"] = _container(command=["worker", "--kind", "repository_repair"])
    extra = assess_business_readiness(
        services, {"status": "ok"}, {"status": "ready"}
    )
    assert extra["status"] == "not_ready"
