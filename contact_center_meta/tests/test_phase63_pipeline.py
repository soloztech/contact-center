import uuid

from odoo.addons.queue_job.tests.common import trap_jobs

from ..services.messaging import atomic_events, sanitize_webhook_envelope
from .common import MetaCase


class TestMetaPhase63Pipeline(MetaCase):
    REMOTE_MESSENGER_ID = "900000000000064"
    REMOTE_INSTAGRAM_ID = "800000000000064"
    TIMESTAMP = 1787688000123

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.technical_author = cls.env["res.partner"].create(
            {"name": "Meta Phase 6.3 External Device"}
        )
        cls.account.technical_author_id = cls.technical_author

    def _instagram_connection(self):
        cached = getattr(self, "_phase63_pipeline_instagram", None)
        if cached:
            return cached
        account = self._create_account(
            "instagram",
            self.INSTAGRAM_ID,
            team=self.team,
        )
        account.technical_author_id = self.technical_author
        connection = self._create_connection(
            account,
            self.instagram_asset,
        )
        self._phase63_pipeline_instagram = connection
        return connection

    def _atomic(
        self,
        carrier,
        *,
        platform="messenger",
        from_me=False,
        locator_references=None,
    ):
        if platform == "messenger":
            object_type = "page"
            asset_id = self.ACTIVE_PAGE_ID
            remote_id = self.REMOTE_MESSENGER_ID
        else:
            object_type = "instagram"
            asset_id = self.INSTAGRAM_ID
            remote_id = self.REMOTE_INSTAGRAM_ID
        sender_id, recipient_id = (
            (asset_id, remote_id) if from_me else (remote_id, asset_id)
        )
        envelope = {
            "object": object_type,
            "entry": [
                {
                    "id": asset_id,
                    "messaging": [
                        {
                            "sender": {"id": sender_id},
                            "recipient": {"id": recipient_id},
                            "timestamp": self.TIMESTAMP,
                            **carrier,
                        }
                    ],
                }
            ],
        }
        sanitized = sanitize_webhook_envelope(
            envelope,
            private_locator_references=locator_references,
        )
        return list(atomic_events(sanitized))[0]

    def _process(self, connection, atomic, suffix):
        inbox = (
            self.env["contact.center.inbox.event"]
            .sudo()
            .with_context(contact_center_skip_enqueue=True)
            .create(
                {
                    "provider_connection_id": connection.id,
                    "inbox_dedupe_key": "meta-phase63:%s:%s" % (suffix, uuid.uuid4()),
                    "provider_schema_version": connection.provider_schema_version,
                    "raw_envelope_json": atomic,
                }
            )
        )
        job_uuid = str(uuid.uuid4())
        inbox.sudo().write({"queue_job_uuid": job_uuid})
        with trap_jobs():
            self.assertTrue(inbox.with_context(job_uuid=job_uuid)._job_process())
        inbox.invalidate_recordset()
        self.assertEqual(inbox.state, "done")
        return inbox

    def _binding(self, connection, external_message_id):
        return (
            self.env["contact.center.message.binding"]
            .sudo()
            .search(
                [
                    ("provider_connection_id", "=", connection.id),
                    ("external_message_id", "=", external_message_id),
                ],
                limit=1,
            )
        )

    def test_postback_projects_interactive_message_and_media_queues_privately(self):
        postback = self._atomic(
            {
                "postback": {
                    "mid": "m_phase63_pipeline_postback",
                    "title": "Acompanhar pedido",
                    "payload": "ORDER_STATUS:synthetic",
                }
            }
        )
        self._process(self.connection, postback, "postback")
        postback_binding = self._binding(self.connection, "m_phase63_pipeline_postback")
        self.assertTrue(postback_binding)
        self.assertEqual(postback_binding.content_type, "interactive")
        self.assertIn("Acompanhar pedido", str(postback_binding.message_id.body))

        locator_ref = "c" * 64
        media = self._atomic(
            {
                "message": {
                    "mid": "m_phase63_pipeline_media",
                    "attachments": [
                        {
                            "type": "image",
                            "payload": {
                                "url": "https://lookaside.example.invalid/private-image"
                            },
                        }
                    ],
                }
            },
            locator_references={(0, "attachment:0"): locator_ref},
        )
        self._process(self.connection, media, "media")
        media_binding = self._binding(self.connection, "m_phase63_pipeline_media")
        self.assertTrue(media_binding)
        self.assertEqual(media_binding.content_type, "image")
        media_record = media_binding.media_ids.ensure_one()
        self.assertEqual(media_record.state, "pending")
        self.assertEqual(
            media_record.remote_locator_json,
            {"private_locator_ref": locator_ref},
        )
        self.assertNotIn("lookaside", str(media_record.remote_locator_json))

    def test_reaction_and_edit_apply_to_the_existing_messenger_message(self):
        target = self._atomic(
            {
                "message": {
                    "mid": "m_phase63_pipeline_mutation_target",
                    "text": "Texto original",
                }
            }
        )
        self._process(self.connection, target, "mutation-target")
        binding = self._binding(self.connection, "m_phase63_pipeline_mutation_target")

        reaction = self._atomic(
            {
                "reaction": {
                    "mid": "m_phase63_pipeline_mutation_target",
                    "action": "react",
                    "reaction": "like",
                    "emoji": "👍",
                }
            }
        )
        reaction_inbox = self._process(self.connection, reaction, "reaction")
        reaction_mutation = (
            self.env["contact.center.message.mutation"]
            .sudo()
            .search(
                [
                    (
                        "external_event_id",
                        "=",
                        reaction_inbox.normalized_dto_json["event_id"],
                    )
                ],
                limit=1,
            )
        )
        self.assertEqual(reaction_mutation.state, "applied")
        self.assertEqual(reaction_mutation.reaction_emoji, "👍")

        edit = self._atomic(
            {
                "message_edit": {
                    "mid": "m_phase63_pipeline_mutation_target",
                    "text": "Texto editado",
                    "num_edit": 1,
                }
            }
        )
        edit_inbox = self._process(self.connection, edit, "edit")
        edit_mutation = (
            self.env["contact.center.message.mutation"]
            .sudo()
            .search(
                [
                    (
                        "external_event_id",
                        "=",
                        edit_inbox.normalized_dto_json["event_id"],
                    )
                ],
                limit=1,
            )
        )
        binding.message_id.invalidate_recordset(["body"])
        self.assertEqual(edit_mutation.state, "applied")
        self.assertIn("Texto editado", str(binding.message_id.body))

    def test_instagram_unsend_and_read_receipt_use_existing_targets(self):
        connection = self._instagram_connection()
        inbound = self._atomic(
            {
                "message": {
                    "mid": "m_phase63_pipeline_ig_inbound",
                    "text": "Mensagem a remover",
                }
            },
            platform="instagram",
        )
        self._process(connection, inbound, "instagram-inbound")
        inbound_binding = self._binding(connection, "m_phase63_pipeline_ig_inbound")

        deleted = self._atomic(
            {"message": {"mid": "m_phase63_pipeline_ig_inbound", "is_deleted": True}},
            platform="instagram",
        )
        self._process(connection, deleted, "instagram-delete")
        inbound_binding.invalidate_recordset(["message_state"])
        self.assertEqual(inbound_binding.message_state, "deleted")

        echo = self._atomic(
            {
                "message": {
                    "mid": "m_phase63_pipeline_ig_outbound",
                    "text": "Resposta pelo Instagram",
                    "is_echo": True,
                }
            },
            platform="instagram",
            from_me=True,
        )
        self._process(connection, echo, "instagram-echo")
        outbound_binding = self._binding(connection, "m_phase63_pipeline_ig_outbound")
        self.assertEqual(outbound_binding.delivery_state, "sent")

        read = self._atomic(
            {"read": {"mid": "m_phase63_pipeline_ig_outbound"}},
            platform="instagram",
        )
        self._process(connection, read, "instagram-read")
        outbound_binding.invalidate_recordset(["delivery_state"])
        self.assertEqual(outbound_binding.delivery_state, "read")

    def test_instagram_edit_revision_prevents_late_regression(self):
        connection = self._instagram_connection()
        target_id = "m_phase63_pipeline_ig_edit_target"
        target = self._atomic(
            {"message": {"mid": target_id, "text": "Texto original"}},
            platform="instagram",
        )
        self._process(connection, target, "instagram-edit-target")
        binding = self._binding(connection, target_id)

        newer = self._atomic(
            {
                "message_edit": {
                    "mid": target_id,
                    "text": "Revisão dois",
                    "num_edit": 2,
                }
            },
            platform="instagram",
        )
        older = self._atomic(
            {
                "message_edit": {
                    "mid": target_id,
                    "text": "Revisão um atrasada",
                    "num_edit": 1,
                }
            },
            platform="instagram",
        )
        self._process(connection, newer, "instagram-edit-newer")
        older_inbox = self._process(connection, older, "instagram-edit-older")

        stale_mutation = (
            self.env["contact.center.message.mutation"]
            .sudo()
            .search(
                [
                    (
                        "external_event_id",
                        "=",
                        older_inbox.normalized_dto_json["event_id"],
                    )
                ],
                limit=1,
            )
        )
        binding.message_id.invalidate_recordset(["body"])
        self.assertIn("Revisão dois", str(binding.message_id.body))
        self.assertEqual(stale_mutation.provider_revision, 1)
        self.assertEqual(
            stale_mutation.details_json["projection"]["reason"], "superseded"
        )

    def test_messenger_delivery_receipt_updates_all_explicit_targets(self):
        for message_id in (
            "m_phase63_pipeline_outbound_one",
            "m_phase63_pipeline_outbound_two",
        ):
            echo = self._atomic(
                {
                    "message": {
                        "mid": message_id,
                        "text": "External-device message",
                        "is_echo": True,
                    }
                },
                from_me=True,
            )
            self._process(self.connection, echo, "echo-%s" % message_id)

        delivery = self._atomic(
            {
                "delivery": {
                    "mids": [
                        "m_phase63_pipeline_outbound_two",
                        "m_phase63_pipeline_outbound_one",
                    ],
                    "watermark": self.TIMESTAMP,
                }
            }
        )
        self._process(self.connection, delivery, "messenger-delivery")

        for message_id in (
            "m_phase63_pipeline_outbound_one",
            "m_phase63_pipeline_outbound_two",
        ):
            binding = self._binding(self.connection, message_id)
            binding.invalidate_recordset(["delivery_state"])
            self.assertEqual(binding.delivery_state, "delivered")

    def test_messenger_read_watermark_updates_the_current_conversation(self):
        for message_id in (
            "m_phase63_pipeline_read_one",
            "m_phase63_pipeline_read_two",
        ):
            echo = self._atomic(
                {
                    "message": {
                        "mid": message_id,
                        "text": "External-device message",
                        "is_echo": True,
                    }
                },
                from_me=True,
            )
            self._process(self.connection, echo, "read-echo-%s" % message_id)

        read = self._atomic({"read": {"watermark": self.TIMESTAMP}})
        self._process(self.connection, read, "messenger-read-watermark")

        for message_id in (
            "m_phase63_pipeline_read_one",
            "m_phase63_pipeline_read_two",
        ):
            binding = self._binding(self.connection, message_id)
            binding.invalidate_recordset(["delivery_state"])
            self.assertEqual(binding.delivery_state, "read")
