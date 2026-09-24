"""Conservative, local scope evidence, independent of retrieved candidates.

This is a bounded language recognizer, not a general semantic model or physical
verification. A named question target can establish instruction applicability;
it does not prove that the machine has reached that stage. Unsupported language
remains mentioned/uncertain. ``supplied`` retains the legacy caller-observation
contract, explicitly labelled as a caller assertion, never verified evidence.
"""

from __future__ import annotations

import re
from typing import Any

from .ids import digest

SCOPE_FACT_POLICY = "observed-polarity-provenance-v2"
SCOPE_KEYS = frozenset({
    "product", "component", "board", "software_version", "boot_stage", "storage_medium",
})


class ScopeFactError(ValueError):
    pass


def _literal(pattern: str) -> str:
    # Unicode word boundaries consider adjacent Chinese part of an ASCII word.
    return rf"(?<![a-z0-9_])(?:{pattern})(?![a-z0-9_])"


_ALIASES = {
    "product": {"k3": "k3"},
    "board": {
        "k3-pico-itx": r"(?:k3[- ]?)?pico(?:[- ]itx)?",
        "evb": r"(?:k3[- ]?)?evb", "board1": "board1",
    },
    "boot_stage": {
        "u-boot": r"u-?boot", "edk2": "edk2", "spl": "spl",
        "opensbi": r"opensbi|open-sbi", "esos": "esos",
        "userspace": r"userspace|user-space|用户态", "brom": "brom",
    },
    "storage_medium": {value: value for value in ("ufs", "nvme", "emmc", "sd", "usb")},
}
_ENTITIES = [
    (field, value, re.compile(_literal(pattern), re.IGNORECASE))
    for field, values in _ALIASES.items() for value, pattern in values.items()
]
_VERSION = re.compile(
    r"(?:(?:软件|固件|software\s+|firmware\s+)?(?:版本|version)\s*"
    r"(?:不是|并非|不再是|是|为|[:=]|is\s+not|is)?\s*)"
    r"(?P<version>[a-z0-9][a-z0-9._+-]{0,63})", re.IGNORECASE,
)
_CLAUSE = re.compile(r"[^，,;；。!！?？\n]+")
_QUOTED = re.compile(r"```[\s\S]*?```|`[^`]*`|‘[^’]*’|“[^”]*”|\"[^\"]*\"|'[^'\n]*'")
_CURRENT = re.compile(
    r"现在|目前|当前|实际|而是|改为|换成|切换到|\b(?:now|currently|actually|instead|but)\b",
    re.IGNORECASE,
)
_HISTORY = re.compile(
    r"以前|之前|原来|曾经|先前|过去|\b(?:previously|previous|formerly|used\s+to|was\s+using|old)\b",
    re.IGNORECASE,
)
_HYPOTHETICAL = re.compile(
    r"如果|假如|假设|计划|准备|打算|想换|将来|未来|建议|推荐|应当|"
    r"\b(?:if|suppose|hypothetical|plan(?:ning)?|would|recommend|should)\b",
    re.IGNORECASE,
)
_UNCERTAIN = re.compile(
    r"不知道|不确定|未确定|没确定|可能|也许|大概|应该是|待确认|是否|还是|"
    r"\b(?:maybe|perhaps|unknown|unsure|whether|either|or|not\s+sure)\b",
    re.IGNORECASE,
)
_NEGATIVE = re.compile(
    r"不是|并非|不再是|尚未|还没|没有|未曾|无法|不用|不使用|(?<!不)没(?:用|进|到)|未(?:使用|进入)|非\s*$|"
    r"\b(?:not|never|without|no|haven't|hasn't|isn't|aren't|cannot|can't)\b",
    re.IGNORECASE,
)
# Negation of a transition is evidence that its destination has not been
# reached, not merely an unclassified stage mention. Keep this relation local
# to the stage so "Pico cannot enter U-Boot" does not negate the board itself.
_FAILED_STAGE_PREFIX = re.compile(
    r"(?:(?:不能(?:够)?|不可(?:以)?|未(?:能|曾)?|没(?:能|有)?|尚未(?:能)?|还没(?:能)?|无法)"
    r"\s*(?:正常|成功|稳定|再)?\s*(?:进入|进到|启动(?:到|进)|到达|运行到|到|进|上)|"
    r"进不去|进不了|到不了|上不了|启动不到|启动不了|"
    r"\b(?:cannot|can't|could\s+not|did\s+not|failed\s+to|unable\s+to|never|haven't|hasn't)"
    r"\s+(?:successfully\s+)?(?:enter|reach|boot(?:\s+(?:into|to))?|get\s+(?:to|into)))\s*$",
    re.IGNORECASE,
)
_FAILED_STAGE_SUFFIX = re.compile(
    r"^\s*(?:进不去|进不了|到不了|上不了|没(?:有)?进入|尚未进入|还没进入|无法进入|"
    r"(?:cannot|can't|could\s+not)\s+be\s+(?:reached|entered)|"
    r"(?:has\s+not|hasn't)\s+been\s+(?:reached|entered))", re.IGNORECASE,
)
_COMPARISON = re.compile(r"区别|对比|比较|相比|哪个更|\b(?:versus|vs\.?|compare|difference|comparison)\b", re.IGNORECASE)
_REFERENCE = re.compile(r"文档|资料|示例|引用|手册|提到|提及|\b(?:documentation|example|quoted|manual|mention(?:ed)?)\b", re.IGNORECASE)
_OBSERVATION = re.compile(
    r"是|使用|用的|运行|卡在|停在|进入|启动到|当前|目前|现在|版本|"
    r"\b(?:is|are|using|running|entered|stuck|current|version)\b", re.IGNORECASE,
)


def _last(pattern, text: str) -> int:
    return max((match.start() for match in pattern.finditer(text)), default=-1)


def _normal(value: str) -> str:
    return value.strip().lower().replace("_", "-")


def _canonical(field: str, value: str) -> str:
    for canonical, pattern in _ALIASES.get(field, {}).items():
        if re.fullmatch(pattern, value.strip(), re.IGNORECASE):
            return canonical
    return _normal(value)


def _status(query: str, clause, start: int, end: int, field: str, quotes) -> tuple[str, str, str]:
    text = clause.group()
    prefix, suffix = query[clause.start():start], query[end:clause.end()]
    line_start = query.rfind("\n", 0, start) + 1
    if any(left <= start and end <= right for left, right in quotes) or query[line_start:start].lstrip().startswith(">"):
        return "mentioned", "reference", "quoted_not_current_observation"
    sentence_start = max(query.rfind(separator, 0, start) for separator in "。!?！？\n") + 1
    discourse = query[sentence_start:start]
    # Quoted examples cannot establish or negate the enclosing message's facts.
    for left, right in quotes:
        if sentence_start <= left < right <= start:
            offset = left - sentence_start
            discourse = discourse[:offset] + " " * (right - left) + discourse[offset + right - left:]
    if _REFERENCE.search(text) or _COMPARISON.search(text) or _REFERENCE.search(discourse):
        return "mentioned", "reference", "reference_or_comparison"
    # A current/correction marker ends history, but does not make an earlier
    # hypothetical antecedent true ("if we switch to NVMe").
    if _HYPOTHETICAL.search(discourse):
        return "hypothetical", "unspecified", "conditional_or_planned"
    if _last(_HISTORY, discourse) > _last(_CURRENT, discourse):
        return "historical", "unspecified", "past_not_current"
    if _UNCERTAIN.search(text):
        return "uncertain", "unspecified", "explicit_uncertainty"
    # An interrogative asking which board/version is present is not an answer
    # to that question. How-to questions naming a target are handled below.
    if (re.match(r"\s*(?:is|are|was|were)\b", text, re.IGNORECASE)
            and clause.end() < len(query) and query[clause.end()] in "?？") or re.match(r"\s*(?:吗|么)", suffix):
        return "uncertain", "unspecified", "identity_question"
    last_current = _last(_CURRENT, prefix)
    local_prefix = prefix[last_current:] if last_current >= 0 else prefix
    if field == "boot_stage" and (
        _FAILED_STAGE_PREFIX.search(local_prefix) or _FAILED_STAGE_SUFFIX.search(suffix)
    ):
        if re.search(r"不是(?:不|没|未|无)|并非(?:不|没|未|无)|\bnot\s+(?:unable|impossible)\b", local_prefix, re.IGNORECASE):
            return "uncertain", "unspecified", "complex_negated_transition"
        return "negated", "unspecified", "stage_transition_not_reached"
    if _NEGATIVE.search(local_prefix):
        # Double negation is deliberately not turned into a positive assertion.
        if len(list(_NEGATIVE.finditer(local_prefix))) > 1 or "不是不" in local_prefix:
            return "uncertain", "unspecified", "complex_negation"
        return "negated", "unspecified", "explicit_negation"
    if re.match(r"\s*(?:不是当前|不是现场|并未使用|is\s+not\s+(?:the\s+)?(?:current|board))", suffix, re.IGNORECASE):
        return "negated", "unspecified", "postposed_negation"
    if field == "storage_medium" and re.match(r"\s*(?:没有接|未接|没接|不存在|is\s+(?:not\s+present|absent))", suffix, re.IGNORECASE):
        return "negated", "unspecified", "absent_medium"
    if field == "boot_stage" and re.search(r"(?:如何|怎么|怎样).*进入|\bhow\b.*\b(?:enter|reach)\b", prefix, re.IGNORECASE):
        return "mentioned", "target", "requested_destination_not_reached_stage"
    if field == "boot_stage":
        if re.search(
            r"(?:进入|卡在|停在|运行于|运行在|启动到|阶段(?:是|为)|(?:现在|目前|当前)(?:处于|是)|"
            r"\b(?:entered|running(?:\s+in)?|stuck(?:\s+at)?|stage\s+is))\s*$", prefix, re.IGNORECASE,
        ):
            return "affirmed", "observed", "explicit_stage_observation"
        if re.search(r"在\s*$|\b(?:in|under|from)\s*$", prefix, re.IGNORECASE) or re.match(r"\s*(?:里|下|中|命令|怎么|如何)", suffix):
            return "affirmed", "target", "named_instruction_target"
        return "mentioned", "unspecified", "stage_mention_is_not_reached_stage"
    if _OBSERVATION.search(prefix):
        return "affirmed", "observed", "explicit_caller_statement"
    return "affirmed", "target", "named_instruction_target"


def analyze_scope_facts(query: str, supplied: dict[str, str] | None = None) -> dict[str, Any]:
    """Return auditable mentions plus a conservative current scope projection.

    Offsets are Python Unicode character offsets into the digest-bound query.
    No candidate, expected label, model confidence, or publication authority is
    accepted. Callers must not pass unvalidated model output as ``supplied``.
    Existing supplied observations are assertions, not independently verified
    measurements; contradictory current text invalidates their applicability.
    """
    if not isinstance(query, str) or len(query) > 32768:
        raise ScopeFactError("query must be a string of at most 32768 characters")
    if supplied is not None and not isinstance(supplied, dict):
        raise ScopeFactError("observed scope must be a mapping")
    if set(supplied or {}) - SCOPE_KEYS or any(
        not isinstance(value, str) or not value.strip() for value in (supplied or {}).values()
    ):
        raise ScopeFactError("scope must contain observed nonempty strings only")
    mentions: list[dict[str, Any]] = []
    query_digest = digest(query)
    for field, value in sorted((supplied or {}).items()):
        mentions.append({
            "field": field, "value": _canonical(field, value), "text": value,
            "status": "affirmed", "role": "observed", "reason": "legacy_caller_observation",
            "source": {"kind": "supplied", "input_digest": digest(supplied), "field": field,
                       "verification": "caller_assertion"},
        })
    quotes = [(match.start(), match.end()) for match in _QUOTED.finditer(query)]
    for clause in _CLAUSE.finditer(query):
        matches = [
            (clause.start() + match.start(), clause.start() + match.end(), field, value)
            for field, value, pattern in _ENTITIES for match in pattern.finditer(clause.group())
        ]
        matches.extend(
            (clause.start() + match.start("version"), clause.start() + match.end("version"), "software_version", _normal(match.group("version")))
            for match in _VERSION.finditer(clause.group())
        )
        for start, end, field, value in sorted(matches):
            status, role, reason = _status(query, clause, start, end, field, quotes)
            mentions.append({
                "field": field, "value": value, "text": query[start:end],
                "status": status, "role": role, "reason": reason,
                "source": {"kind": "query", "input_digest": query_digest, "start": start,
                           "end": end, "verification": "caller_statement"},
            })
    fields, scope = {}, {}
    for field in sorted({mention["field"] for mention in mentions}):
        indexes = [index for index, mention in enumerate(mentions) if mention["field"] == field]
        positive = {mentions[index]["value"] for index in indexes if mentions[index]["status"] == "affirmed"}
        negative = {mentions[index]["value"] for index in indexes if mentions[index]["status"] == "negated"}
        if len(positive) > 1 or positive & negative:
            state, value = "conflict", None
        elif len(positive) == 1:
            state, value = "known", next(iter(positive))
            scope[field] = value
            # Preserve legacy spelling (e.g. product K3) for persisted input and
            # Gold contracts; comparisons/mentions remain canonical. This does
            # not choose a supplied value over conflicting evidence above.
            declared = (supplied or {}).get(field)
            if declared is not None and _normal(declared) == value:
                scope[field] = declared.strip()
        else:
            state, value = "unknown", None
        fields[field] = {"state": state, "value": value, "excluded_values": sorted(negative), "mention_indexes": indexes}
    return {
        "schema_version": 1, "policy": SCOPE_FACT_POLICY, "query_digest": query_digest,
        "supplied_digest": digest(supplied), "mentions": mentions, "fields": fields,
        "observed_scope": scope,
    }
