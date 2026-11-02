"""Policy citation and version resolution."""
from datetime import datetime
from typing import Optional
from sqlalchemy import select

from .db import PolicyDocument

# Mapping of business reason codes to corresponding policy sections
SECTION_FOR = {
    "REFUND_SUCCEEDED": "section_4",
    "REFUND_IN_PROGRESS": "section_4",
    "NO_RETURN": "section_2",
    "RETURN_REJECTED": "section_2",
    "RETURN_NOT_RECEIVED": "section_2",
    "OUTSIDE_WINDOW": "section_1",
    "INSPECTION_PENDING": "section_2",
    "INSPECTION_FAILED": "section_2",
    "RETURN_RECEIVED_INSPECTION_PASSED": "section_3",
    "DAMAGED_ITEM": "section_1",
    "REPLACEMENT_DISPATCHED": "section_2",
}


def find_policy(session, kind: str, on_date: datetime) -> Optional[PolicyDocument]:
    """Retrieve the policy active at the given timestamp for a specific policy category."""
    query = (
        select(PolicyDocument)
        .where(PolicyDocument.kind == kind)
        .order_by(PolicyDocument.effective_from.desc())
    )
    docs = session.scalars(query).all()
    for d in docs:
        if d.effective_from <= on_date and (d.effective_to is None or on_date < d.effective_to):
            return d
    return None


def cite(doc: Optional[PolicyDocument], reason_code: str) -> str:
    """Generate formal reference code for citations in proposals and customer replies."""
    if not doc:
        return "apex_standard_policy:section_1"
    section = SECTION_FOR.get(reason_code, "section_1")
    return f"{doc.id}:{section}"
