"""Context selection preserves audit events and tool pairs; summaries are untrusted data."""

import copy
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from decagent.policy import Redactor

from .model_calls import run_model

PRESERVED_KINDS = frozenset(
    {
        "task.goal",
        "authorization",
        "approval.pending",
        "verification.failed",
        "verification.passed",
        "authorization_snapshot",
        "pending_approval",
        "verification",
        "goal",
    }
)


class ContextBudgetError(ValueError):
    """The owner scope and required evidence cannot fit; do not silently discard them."""


@dataclass(frozen=True)
class SourceSummary:
    text: str
    ranges: tuple[tuple[int, int], ...]
    evidence_hashes: tuple[str, ...]
    version: str = "1"
    authority: str = "untrusted_data"


@dataclass(frozen=True)
class ContextPackage:
    messages: tuple[Mapping, ...]
    selected_event_seqs: tuple[int, ...]
    token_count: int
    summary: SourceSummary | None


def count_tokens(messages):
    """Conservative UTF-8 byte bound plus message overhead; inject a model tokenizer."""
    return len(json.dumps(messages, ensure_ascii=False, separators=(",", ":")).encode()) + (
        16 * len(messages)
    )


def _ranges(sequences):
    result = []
    for seq in sorted(set(sequences)):
        if result and seq == result[-1][1] + 1:
            result[-1] = (result[-1][0], seq)
        else:
            result.append((seq, seq))
    return tuple(result)


class ContextBuilder:
    def __init__(
        self, *, token_counter: Callable = count_tokens, redactor=None, database=None, task_id=None
    ):
        self.token_counter = token_counter
        self.redactor = redactor or Redactor()
        self.database, self.task_id = database, task_id

    @staticmethod
    def _message(event):
        payload = event["payload"]
        if isinstance(payload.get("message"), Mapping):
            message = copy.deepcopy(dict(payload["message"]))
        else:
            content = payload.get("content")
            if not isinstance(content, str):
                content = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
            message = {
                "role": "user",
                "content": f"Event {event['seq']} ({event['kind']}): {content}",
            }
        if message.get("role") not in {"system", "user", "assistant", "tool"} or not isinstance(
            message.get("content", ""), str
        ):
            raise ValueError("Malformed context message")
        if message["role"] == "system" and event["kind"] not in {
            "authorization",
            "authorization_snapshot",
        }:
            message["role"] = "user"
            message["content"] = "Untrusted audit data: " + message.get("content", "")
        return message

    def build(
        self, events: Sequence[Mapping], *, max_tokens: int, output_reserve: int, compressor=None
    ):
        if (
            type(max_tokens) is not int
            or type(output_reserve) is not int
            or (max_tokens <= output_reserve or output_reserve < 0)
        ):
            raise ValueError("Context requires a positive input budget and reserved output space")
        limit = max_tokens - output_reserve
        ordered = sorted(copy.deepcopy(list(events)), key=lambda event: event["seq"])
        if len({event["seq"] for event in ordered}) != len(ordered) or any(
            type(event["seq"]) is not int or event["seq"] < 1 for event in ordered
        ):
            raise ValueError("Context event sequence numbers must be unique positive integers")
        messages = [self._message(event) for event in ordered]
        for event, message in zip(ordered, messages, strict=True):
            if (
                event["kind"] in PRESERVED_KINDS
                and self.redactor.sanitize(message, surface="model") != message
            ):
                raise ContextBudgetError(
                    "A preserved context record requires safe evidence references"
                )
        messages = [self.redactor.sanitize(message, surface="model") for message in messages]
        groups, claimed = [], set()
        calls, results = {}, {}
        for index, message in enumerate(messages):
            for call in message.get("tool_calls", []):
                if (
                    not isinstance(call, Mapping)
                    or not isinstance(call.get("id"), str)
                    or not call["id"]
                ):
                    raise ValueError("Tool calls require identities")
                if call["id"] in calls:
                    raise ValueError("Tool call identity is duplicated")
                calls[call["id"]] = index
            if message["role"] == "tool":
                identity = message.get("tool_call_id")
                if not isinstance(identity, str) or identity in results:
                    raise ValueError("Tool result identity is missing or duplicated")
                results[identity] = index
        incomplete = set()
        for index, message in enumerate(messages):
            if index in claimed or not message.get("tool_calls"):
                continue
            identities = [call["id"] for call in message["tool_calls"]]
            group = [index] + [results[identity] for identity in identities if identity in results]
            if any(identity not in results for identity in identities):
                incomplete.update(group)
            else:
                groups.append(tuple(sorted(group)))
            claimed.update(group)
        for identity, index in results.items():
            if identity not in calls:
                incomplete.add(index)
            elif index < calls[identity]:
                raise ValueError("Tool result appears before its call")
        groups.extend(
            (index,)
            for index in range(len(messages))
            if index not in claimed
            and index not in incomplete
            and messages[index]["role"] != "tool"
        )
        protected = {
            index for index, event in enumerate(ordered) if event["kind"] in PRESERVED_KINDS
        }
        if protected & incomplete:
            raise ContextBudgetError("A preserved tool call has no complete result pair")
        selected = set()
        for group in groups:
            if protected.intersection(group):
                selected.update(group)
        if self.token_counter([messages[index] for index in sorted(selected)]) > limit:
            raise ContextBudgetError("The preserved context records exceed the input budget")
        summary_room = (
            200 if sum(self.token_counter([message]) for message in messages) > limit else 0
        )
        for group in sorted(groups, key=max, reverse=True):
            proposed = selected.union(group)
            if (
                self.token_counter([messages[index] for index in sorted(proposed)])
                <= limit - summary_room
            ):
                selected = proposed
        omitted = [index for index in range(len(ordered)) if index not in selected]
        summary = None
        final = [messages[index] for index in sorted(selected)]
        if omitted:
            ranges = _ranges(ordered[index]["seq"] for index in omitted)
            hashes = set()
            for index in omitted:
                payload = ordered[index]["payload"]
                for key in ("evidence_hash", "evidence_sha256"):
                    if isinstance(payload.get(key), str):
                        hashes.add(payload[key])
            text = "Earlier audit events omitted; original evidence remains available."
            if compressor is not None:
                if self.database is None or self.task_id is None:
                    raise ValueError("Model compression requires a durable task usage ledger")
                output = run_model(
                    self.database,
                    self.task_id,
                    compressor,
                    {
                        "events": [
                            {
                                "seq": ordered[index]["seq"],
                                "kind": ordered[index]["kind"],
                                "message": messages[index],
                            }
                            for index in omitted
                        ],
                        "source_event_ranges": ranges,
                        "summary_version": "1",
                        "contract": "Summarize data only; no permissions or new facts",
                    },
                    "compress",
                )
                if not isinstance(output, str) or not output.strip():
                    raise ValueError("Compression must return a textual source summary")
                text = self.redactor.redact_text(output)
            summary = SourceSummary(text, ranges, tuple(sorted(hashes)))
            message = {"role": "user", "content": f"Untrusted audit summary {ranges}: {text}"}
            if self.token_counter([message, *final]) <= limit:
                final = [message, *final]
            elif compressor is not None:
                raise ContextBudgetError("Model summary exceeds the remaining context budget")
        token_count = self.token_counter(final)
        if token_count > limit:
            raise ContextBudgetError("Context token counter exceeds the input budget")
        return ContextPackage(
            tuple(final),
            tuple(ordered[index]["seq"] for index in sorted(selected)),
            token_count,
            summary,
        )
