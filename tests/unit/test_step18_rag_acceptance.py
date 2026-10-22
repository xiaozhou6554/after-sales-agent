from bootstrap.verify_full_rag import assess_case


def test_answerable_requires_expected_document_and_accepted_evidence() -> None:
    case = {"expected_outcome": "answerable", "document_ids": ["expected"]}
    assert assess_case(case, ["expected"], True) == "passed"
    assert assess_case(case, ["other"], True) == "failed"
    assert assess_case(case, ["expected"], False) == "failed"


def test_no_answer_requires_no_accepted_evidence() -> None:
    case = {"expected_outcome": "no_answer", "document_ids": []}
    assert assess_case(case, [], False) == "passed"
    assert assess_case(case, ["irrelevant"], True) == "failed"


def test_version_sensitive_case_remains_manual_review() -> None:
    case = {"expected_outcome": "version_sensitive", "document_ids": ["current", "legacy"]}
    assert assess_case(case, ["current"], True) == "manual_version_review_required"
