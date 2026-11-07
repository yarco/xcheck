"""Review target: omitted settings must retain their previous values."""


def update_preferences(current: dict, changes: dict) -> dict:
    """Return updated preferences without changing either input.

    An omitted key keeps its old value. An explicit None is a stored value,
    not a request to delete the key. New keys are allowed.
    """
    return {**current, **changes}
