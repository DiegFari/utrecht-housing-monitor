from pathlib import Path
import tempfile
import unittest

from scripts.package_public import audit, public_files


class PublicPackageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / "housing.py"
        self.source.write_text("print('public source')\n")

    def test_private_files_and_diagnostics_are_not_selected(self):
        (self.root / ".env").write_text("SMTP_PASSWORD=private-local-test-value\n")
        (self.root / "data").mkdir()
        (self.root / "data/diagnostics.html").write_text("private page")
        (self.root / "private_notes.txt").write_text("private note")
        self.assertEqual(public_files(self.root), [self.source])
        audit(public_files(self.root), self.root)

    def test_copied_gmail_address_blocks_publication(self):
        (self.root / ".env").write_text("SMTP_USER=private-test@example.com\n")
        self.source.write_text("contact='private-test@example.com'\n")
        with self.assertRaisesRegex(ValueError, "Possible credential"):
            audit([self.source], self.root)

    def test_app_password_with_removed_spaces_blocks_publication(self):
        (self.root / ".env").write_text("SMTP_PASSWORD='aaaa bbbb cccc dddd'\n")
        self.source.write_text("password='aaaabbbbccccdddd'\n")
        with self.assertRaisesRegex(ValueError, "Possible credential"):
            audit([self.source], self.root)

    def test_symlink_cannot_smuggle_local_secrets(self):
        secret = self.root / ".env"
        secret.write_text("SMTP_PASSWORD=local-secret\n")
        link = self.root / "fizz.py"
        link.symlink_to(secret)
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            audit([link], self.root)


if __name__ == "__main__":
    unittest.main()
