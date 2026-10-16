"""Version-bound views of program-owned business question definitions."""

import json
from dataclasses import dataclass

from decagent.business.questions import (
    QUESTION_FORMAT_VERSION,
    BusinessQuestionsUnavailable,
    build_business_request,
)
from decagent.decisions.contracts import QuestionSpec, canonical_json, digest

STATE_FIELDS = ("instruction", "directory", "ticket", "sources")
MAX_CANDIDATES = 256


@dataclass(frozen=True)
class QuestionRegistry:
    """A catalogue is a definition source, never a calibration or authorization."""

    spec: object
    _catalog: str

    @classmethod
    def from_spec(cls, spec):
        if spec.level != "enhanced":
            return cls(spec, "[]")
        sample = {
            "directory": {"people": [], "categories": []},
            "ticket": None,
            "sources": {},
        }
        try:
            request = build_business_request(spec, "catalog", "catalog", "catalog", sample)
            questions = {q.id: q for q in request.questions}
        except BusinessQuestionsUnavailable:
            questions = {}
        version = spec.manifest["workflow"]["version"] + "-" + QUESTION_FORMAT_VERSION
        catalog = []
        for definition in spec.manifest["workflow"]["questions"]:
            question = questions.get(definition["id"])
            if question is None:
                continue
            criteria = question.to_wire()["criteria"]
            if definition["type"] == "choice":
                criteria = {
                    name: spec.commands[name].description for name in definition["candidates"]
                }
                criteria["no_match"] = "No legal candidate matches the requested operation"
            catalog.append(
                {
                    "question_id": question.id,
                    "version": question.version.replace("/", "-"),
                    "type": question.type,
                    "instructions": question.instructions,
                    "criteria": criteria,
                    "state_fields": list(STATE_FIELDS),
                    "candidate_source": None,
                    "output_fields": cls._output_fields(question.type),
                    "activation": "requires_matching_policy",
                }
            )
        used = {q["question_id"] for q in catalog}
        for name, rows_key in (("person", "people"), ("category", "categories")):
            question_id = name
            if question_id in used:
                question_id = "directory_" + name
                suffix = 2
                while question_id in used:
                    question_id = "directory_" + name + "_" + str(suffix)
                    suffix += 1
            used.add(question_id)
            catalog.append(
                {
                    "question_id": question_id,
                    "version": version,
                    "type": "choice",
                    "instructions": (
                        f"Select the requested {name} from the actual query candidates. "
                        "Do not invent IDs. If no candidate matches, several names match "
                        "without sufficient disambiguation, or the user did not specify "
                        "enough information, select no_match."
                    ),
                    "criteria": None,
                    "state_fields": list(STATE_FIELDS),
                    "candidate_source": "directory." + rows_key,
                    "output_fields": cls._output_fields("choice"),
                    "activation": "requires_matching_policy",
                }
            )
        return cls(spec, canonical_json(catalog))

    @staticmethod
    def _output_fields(kind):
        return {
            "choice": {
                "choice": "string",
                "confidence": "number",
                "probabilities": "object",
                "selected_value": "string",
            },
            "noul": {"noul": "number", "selected_value": "boolean"},
            "score": {
                "score": "number",
                "confidence": "number",
                "probabilities": "object",
                "legend": "object",
                "selected_value": "number",
            },
        }[kind]

    @property
    def fingerprint(self):
        return digest({"plugin": self.spec.digest, "catalog": json.loads(self._catalog)})

    def describe(self):
        return json.loads(self._catalog)

    def get(self, question_id, version=None):
        for item in self.describe():
            if item["question_id"] == question_id:
                if version is not None and version != item["version"]:
                    raise ValueError("Question version is not registered")
                return item
        raise ValueError("Question definition is not registered")

    def validate_reference(self, reference):
        if not isinstance(reference, dict) or set(reference) != {"question_id", "version", "type"}:
            raise ValueError("Question definition overrides are forbidden")
        current = self.get(reference["question_id"], reference["version"])
        if current["type"] != reference["type"]:
            raise ValueError("Question definition primitive cannot be changed")
        return current

    def render(self, question_id, state, *, node_id, version=None):
        definition = self.get(question_id, version)
        if not isinstance(state, dict) or set(state) - set(STATE_FIELDS):
            raise ValueError("State contains fields outside the trusted template")
        instruction = state.get("instruction")
        if not isinstance(instruction, str) or not instruction.strip():
            raise ValueError("Question requires actual user instruction")
        evidence = {key: state.get(key) for key in STATE_FIELDS if key != "instruction"}
        evidence["directory"] = evidence["directory"] or {}
        evidence["sources"] = evidence["sources"] or {}
        try:
            request = build_business_request(self.spec, "render", "render", instruction, evidence)
        except BusinessQuestionsUnavailable as exc:
            raise ValueError("No admissible current candidate or question") from exc
        selected = next((q for q in request.questions if q.id == question_id), None)
        if selected is None:
            raise ValueError("No admissible current candidate or question")
        if (
            selected.version.replace("/", "-") != definition["version"]
            or selected.type != definition["type"]
        ):
            raise ValueError("Rendered question differs from registered definition")
        return QuestionSpec(
            node_id,
            definition["version"],
            selected.type,
            selected.instructions,
            selected.criteria,
            selected.state_fields,
        )
