from app.tokens import new_resume_token, new_session_id


def test_session_ids_are_unique_and_reasonably_long():
    a, b = new_session_id(), new_session_id()
    assert a != b
    assert len(a) >= 16


def test_resume_tokens_are_unique_and_reasonably_long():
    a, b = new_resume_token(), new_resume_token()
    assert a != b
    assert len(a) >= 32
