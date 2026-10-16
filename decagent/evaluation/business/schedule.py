"""Three rounds, paired cases, balanced strategy position, no parallel database writers."""

STRATEGIES = ("rule", "main", "hybrid")


def make_schedule(cases, rounds=3):
    if not cases or rounds not in {1, 3} or len({c["id"] for c in cases}) != len(cases):
        raise ValueError("Unique cases and one probe or three comparison rounds are required")
    trials = []
    for round_index in range(rounds):
        shift = (round_index * 17) % len(cases)
        ordered = cases[shift:] + cases[:shift]
        for case in ordered:
            original_position = next(i for i, item in enumerate(cases) if item["id"] == case["id"])
            offset = (round_index + original_position) % 3
            strategies = STRATEGIES[offset:] + STRATEGIES[:offset]
            trials.extend(
                {
                    "id": f"{case['id']}/r{round_index + 1}/{strategy}",
                    "case_id": case["id"],
                    "kind": case["kind"],
                    "round": round_index + 1,
                    "strategy": strategy,
                }
                for strategy in strategies
            )
    return trials
