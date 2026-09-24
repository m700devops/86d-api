"""Sent mail lands in the mailbox's Sent folder.

SMTP only sends: three emails reached their recipients (one replied) and none
was in the operator's Sent folder, because nothing ever put a copy there.
mailer.save_to_sent() does, over IMAP, after the send. Fakes stand in for both
servers.
"""
import mailer


class _Imap:
    def __init__(self, folders, append_ok=True):
        self.folders, self.append_ok, self.appended = folders, append_ok, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, user, pw):
        pass

    def list(self):
        return "OK", [f.encode() for f in self.folders]

    def append(self, folder, flags, when, data):
        self.appended.append((folder, flags))
        return ("OK" if self.append_ok else "NO"), [b""]


def _patch(monkeypatch, imap):
    monkeypatch.setattr(mailer.imaplib, "IMAP4_SSL", lambda *a, **k: imap)
    monkeypatch.setattr(mailer, "USER", "Stephan@my86d.com")
    monkeypatch.setattr(mailer, "PASSWORD", "x")


def _msg():
    from email.message import EmailMessage
    m = EmailMessage()
    m["Subject"] = "hi"
    m.set_content("hello")
    return m


def test_the_copy_goes_in_the_folder_the_server_flags_as_sent(monkeypatch):
    imap = _Imap(['(\\HasNoChildren) "." "INBOX"',
                  '(\\HasNoChildren \\Sent) "." "Sent Items"'])
    _patch(monkeypatch, imap)
    assert mailer.save_to_sent(_msg()) == "Sent Items"
    assert imap.appended == [('"Sent Items"', "\\Seen")]


def test_without_a_flag_the_usual_names_are_tried(monkeypatch):
    imap = _Imap(['(\\HasNoChildren) "." "INBOX"', '(\\HasNoChildren) "." "INBOX.Sent"'])
    _patch(monkeypatch, imap)
    assert mailer.save_to_sent(_msg()) == "INBOX.Sent"


def test_a_failed_copy_never_turns_a_sent_email_into_an_error(monkeypatch):
    def boom(*a, **k):
        raise OSError("imap down")
    monkeypatch.setattr(mailer.imaplib, "IMAP4_SSL", boom)
    assert mailer.save_to_sent(_msg()) is None


def test_send_reports_where_the_copy_went(monkeypatch):
    class _Smtp:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def login(self, *a): pass
        def send_message(self, m): self.sent = m
    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", _Smtp)
    monkeypatch.setattr(mailer, "PORT", 465)
    _patch(monkeypatch, _Imap(['(\\Sent) "." "Sent"']))
    out = mailer.send("brent@bar.example", "following up", "Hi Brent")
    assert out["saved_to"] == "Sent" and out["to"] == "brent@bar.example"
