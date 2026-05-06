# internal helpers shared across tool modules

from typing import Any


def drop_none(**kwargs: Any) -> dict[str, Any]:
    """
    Return a dict containing only the kwargs whose value is not None.

    Used at docker-py call sites where None means "let docker-py pick the default"
    and passing the key explicitly with value=None would override that default.
    """
    return {k: v for k, v in kwargs.items() if v is not None}
