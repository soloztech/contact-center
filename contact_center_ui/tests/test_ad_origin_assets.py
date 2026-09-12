"""Exercise the installed Odoo asset graph and its real XML inheritance loader."""

from lxml import etree

from odoo.tests import tagged
from odoo.tests.common import SavepointCase


@tagged("post_install", "-at_install", "contact_center_ad_origin")
class TestAdOriginAssets(SavepointCase):
    def test_backend_bundle_applies_preview_extensions_and_transpiles_module(self):
        qweb = self.env["ir.qweb"]
        files, _remains = qweb._get_asset_content("web.assets_backend")
        paths = [item["url"] for item in files]
        prefix = "/contact_center_ui/static/src/"
        extension = prefix + "xml/ad_origin_preview.xml"
        self.assertEqual(paths.count(extension), 1)
        for target in ("message_content.xml", "attribution_touchpoints.xml"):
            target_path = prefix + "xml/" + target
            self.assertEqual(paths.count(target_path), 1)
            self.assertLess(paths.index(target_path), paths.index(extension))

        # Use every installed backend XML in the order resolved by ir.asset.
        # This fails on duplicate templates, missing parents or invalid XPath;
        # manually expanding only our files would miss those integration bugs.
        bundle = qweb._get_asset_bundle(
            "web.assets_backend", files, env=self.env, css=False, js=True
        )
        document = etree.fromstring(
            ("<templates>" + bundle.xml() + "</templates>").encode()
        )
        payloads = document.xpath("./t[@t-name='contact_center_ui.MessagePayload']")
        self.assertEqual(len(payloads), 1)
        payload = payloads[0]
        cards = payload.xpath(".//MessageAdOriginPreviews")
        self.assertEqual(len(cards), 1)
        original_body = payload.xpath(".//p[@t-if='message.body_text']")
        self.assertEqual(len(original_body), 1)
        self.assertEqual(original_body[0].getnext(), cards[0])
        self.assertEqual(len(payload.xpath(".//AudioPlayer")), 1)
        self.assertEqual(len(payload.xpath(".//AudioTranscription")), 1)
        self.assertEqual(
            len(
                document.xpath(
                    "./t[@t-name='contact_center_ui.AttributionTouchpoints']"
                    "//AdOriginPreview"
                )
            ),
            1,
        )
        self.assertEqual(
            len(document.xpath("./t[@t-name='contact_center_ui.AdOriginPreview']")),
            1,
        )

        # Resolve and transpile the real module through Odoo's JS asset object.
        # Avoid generating/minifying unrelated backend JS or creating attachments.
        scripts = {
            item.url: item for item in bundle.javascripts if item.url.startswith(prefix)
        }
        preview_script = scripts[prefix + "js/ad_origin_preview.esm.js"]
        self.assertTrue(preview_script.is_transpiled)
        self.assertIn("odoo.define(", preview_script.content)
        for dependency in (
            "message_content",
            "attribution_touchpoints",
            "contact_center_model",
        ):
            self.assertIn(prefix + "js/" + dependency + ".esm.js", scripts)
            self.assertIn(
                "@contact_center_ui/js/" + dependency + ".esm",
                preview_script.content,
            )
        tests, _remains = qweb._get_asset_content("web.qunit_suite_tests")
        self.assertEqual(
            [item["url"] for item in tests].count(
                "/contact_center_ui/static/tests/ad_origin_preview_tests.esm.js"
            ),
            1,
        )
