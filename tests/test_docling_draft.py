import copy

import pytest
import yaml
from test_docling_review import evidence

from k3_support.docling_draft import build_draft
from k3_support.professional_knowledge import validate_article


def draft(**kwargs):
    return build_draft(
        evidence=evidence(),
        title="Synthetic draft",
        question="Synthetic question?",
        answer="Unverified authored answer",
        references=["#/texts/0"],
        **kwargs,
    )


def test_draft_uses_professional_schema_and_has_no_approval():
    result = draft(risk_class="persistent", rollback="Restore synthetic fixture")
    metadata = yaml.safe_load(result["markdown"].split("---")[1])
    assert metadata["status"] == "captured"
    assert metadata["review"] is None and metadata["quality"] is None
    assert not metadata["publication"]["automatic_reply"]
    assert metadata["sources"][0]["authority"] == 0
    assert metadata["sources"][0]["locator"]["source_verified"] is False
    assert metadata["claims"][0]["risk_class"] == "persistent"
    promoted = copy.deepcopy(metadata)
    promoted["status"] = "published"
    with pytest.raises(ValueError):
        validate_article(promoted, "Unverified authored answer")


@pytest.mark.parametrize("risk", [None, "", "unresolved", "safe"])
def test_risk_must_be_explicit(risk):
    with pytest.raises(ValueError):
        draft(risk_class=risk)


def test_source_must_belong_to_document():
    with pytest.raises(ValueError):
        build_draft(
            evidence=evidence(),
            title="t",
            question="q",
            answer="a",
            references=["#/texts/999"],
            risk_class="read_only",
        )
