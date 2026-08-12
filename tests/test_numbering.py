from app.orders.numbering import public_number


def test_public_number_format():
    assert public_number(1) == "ORDER-1001"
    assert public_number(48) == "ORDER-1048"
