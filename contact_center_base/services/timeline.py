"""Chronological presentation keys, independent from message ingestion IDs."""

import datetime

from odoo.osv import expression

# Match PostgreSQL's native date ASC NULLS LAST / DESC NULLS FIRST ordering.
# Native messages normally have a date, but keep malformed legacy rows stable.
MISSING_MESSAGE_DATE = datetime.datetime(9999, 12, 31, 23, 59, 59)


def message_chronology_key(message):
    """Return the stable date/ID ordering key of one persisted message."""

    message.ensure_one()
    return message.date or MISSING_MESSAGE_DATE, message.id


def chronology_domain(message, operator, prefix=""):
    """Build an ORM seek bound using a message ID only as a timestamp tie-breaker."""

    if operator not in ("<", "<=", ">", ">="):
        raise ValueError("Unsupported chronological comparison")
    message.ensure_one()
    date_field = prefix + "date"
    id_field = prefix + "id"
    same_date = [(date_field, "=", message.date), (id_field, operator, message.id)]
    if message.date:
        clauses = [[(date_field, operator[0], message.date)], same_date]
        if operator.startswith(">"):
            clauses.append([(date_field, "=", False)])
        return expression.OR(clauses)
    if operator.startswith("<"):
        return expression.OR([[(date_field, "!=", False)], same_date])
    return same_date
