import unittest
from email.message import EmailMessage

from mail_icloud import (
    browser_headers,
    encode_imap_folder,
    extract_verification_code,
    message_timestamp,
    message_matches_target,
    normalize_cookie,
    parse_imap_folders,
)


class ICloudProtocolTests(unittest.TestCase):
    def test_global_headers_match_expected_origin_and_cookie(self):
        headers = browser_headers("global", "  a=b\n")
        self.assertEqual(headers["Origin"], "https://www.icloud.com")
        self.assertEqual(headers["Referer"], "https://www.icloud.com/")
        self.assertEqual(headers["Cookie"], "a=b")

    def test_china_headers_swap_origin_and_locale(self):
        headers = browser_headers("china", "cookie=value")
        self.assertEqual(headers["Origin"], "https://www.icloud.com.cn")
        self.assertIn("zh-CN", headers["Accept-Language"])

    def test_normalize_cookie_accepts_cookie_header(self):
        self.assertEqual(normalize_cookie("cookie: foo=bar; baz=qux"), "foo=bar; baz=qux")

    def test_extract_verification_code_prefers_keyword_window(self):
        body = "Your verification code is 123456. Ignore year 2026."
        self.assertEqual(extract_verification_code("OpenAI code", body), "123456")

    def test_message_matches_target_in_recipient_headers(self):
        msg = EmailMessage()
        msg["To"] = "account@icloud.com"
        self.assertTrue(message_matches_target(msg, "body", "account@icloud.com"))
        self.assertFalse(message_matches_target(msg, "body", "other@icloud.com"))

    def test_message_matches_target_in_forwarding_headers(self):
        msg = EmailMessage()
        msg["X-Original-To"] = "hme-one@icloud.com"
        msg["Delivered-To"] = "forward-inbox@example.com"
        self.assertTrue(message_matches_target(msg, "code 123456", "hme-one@icloud.com"))
        self.assertFalse(message_matches_target(msg, "code 123456", "hme-two@icloud.com"))

    def test_parse_imap_folders_accepts_common_separators(self):
        self.assertEqual(
            parse_imap_folders("INBOX, Junk\nSpam; 垃圾邮件,INBOX"),
            ["INBOX", "Junk", "Spam", "垃圾邮件"],
        )
        self.assertEqual(parse_imap_folders(""), ["INBOX"])

    def test_encode_imap_folder_uses_modified_utf7_for_unicode(self):
        self.assertEqual(encode_imap_folder("INBOX"), "INBOX")
        self.assertEqual(encode_imap_folder("A&B"), "A&-B")
        self.assertEqual(encode_imap_folder("垃圾邮件"), "&V4NXPpCuTvY-")

    def test_message_timestamp_parses_rfc5322_date(self):
        msg = EmailMessage()
        msg["Date"] = "Mon, 03 Aug 2026 12:00:00 +0000"
        self.assertEqual(message_timestamp(msg), 1785758400.0)


if __name__ == "__main__":
    unittest.main()
