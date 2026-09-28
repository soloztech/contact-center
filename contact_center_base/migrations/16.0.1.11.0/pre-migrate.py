"""Add the conversation lifecycle counter without rewriting ``mail_channel``.

With a constant default PostgreSQL stores the value in the catalog, so existing
rows read 0 immediately; letting the ORM create the column would instead update
every channel row to its default.
"""


def migrate(cr, version):
    if version:
        cr.execute(
            "ALTER TABLE mail_channel "
            "ADD COLUMN IF NOT EXISTS contact_center_lifecycle_seq integer DEFAULT 0"
        )
