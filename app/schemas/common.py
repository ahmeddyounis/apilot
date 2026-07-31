"""Shared schema building blocks."""

from decimal import Decimal
from typing import Annotated

from pydantic import PlainSerializer

# Money and quantities serialize as strings in JSON — never floats.
DecimalOut = Annotated[
    Decimal,
    PlainSerializer(lambda value: format(value, "f"), return_type=str, when_used="json"),
]
