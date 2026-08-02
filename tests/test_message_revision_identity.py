import sys
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from edge_agent_task_identity import (  # noqa: E402
    attachment_change_status,
    attachment_identity,
    revision_id,
)


class MessageRevisionIdentityTests(unittest.TestCase):
    def attachment(self, content):
        return attachment_identity(
            file_unique_id="unique-1",
            file_id_hash="file-id-hash",
            content_hash_value=content,
            size=10,
            mime_type="application/pdf",
        )

    def test_same_edit_replays_same_revision(self):
        first = revision_id("root", message_edit_version=1, body_hash="body-a", attachments=[self.attachment("content-a")])
        second = revision_id("root", message_edit_version=1, body_hash="body-a", attachments=[self.attachment("content-a")])
        self.assertEqual(first, second)

    def test_edit_or_attachment_change_creates_new_revision(self):
        first = revision_id("root", message_edit_version=1, body_hash="body-a", attachments=[self.attachment("content-a")])
        edited = revision_id("root", message_edit_version=2, body_hash="body-b", attachments=[self.attachment("content-b")])
        self.assertNotEqual(first, edited)
        self.assertEqual(attachment_change_status([self.attachment("content-a")], [self.attachment("content-b")]), "CHANGED")

    def test_missing_content_hash_is_unknown(self):
        unknown = self.attachment(None)
        self.assertEqual(attachment_change_status([unknown], [unknown]), "UNKNOWN")

    def test_empty_attachment_sets_are_same(self):
        self.assertEqual(attachment_change_status([], []), "SAME")


if __name__ == "__main__":
    unittest.main()
