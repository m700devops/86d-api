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


# ── the page is told whether the copy landed, and why not ───────────────────
# A refused copy used to reach only the server log, so "Sent to …" on screen
# and an empty Sent folder looked like the fix hadn't shipped.

def test_a_refused_copy_says_why(monkeypatch):
    class _Refusing(_Imap):
        def append(self, folder, flags, when, data):
            return "NO", [b"[OVERQUOTA] Mailbox is full"]
    _patch(monkeypatch, _Refusing(['(\\Sent) "." "Sent"']))
    folder, why = mailer.file_copy(_msg())
    assert folder is None and "Mailbox is full" in why and "Sent" in why


def test_no_sent_folder_says_so(monkeypatch):
    _patch(monkeypatch, _Imap(['(\\HasNoChildren) "." "INBOX"', '() "." "Archive"']))
    folder, why = mailer.file_copy(_msg())
    assert folder is None and "no Sent folder" in why


def test_a_login_failure_names_the_server(monkeypatch):
    def boom(*a, **k):
        raise OSError("connection refused")
    monkeypatch.setattr(mailer.imaplib, "IMAP4_SSL", boom)
    folder, why = mailer.file_copy(_msg())
    assert folder is None and mailer.IMAP_HOST in why and "connection refused" in why


def test_send_carries_the_copy_error_to_the_page(monkeypatch):
    class _Smtp:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def login(self, *a): pass
        def send_message(self, m): pass
    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", _Smtp)
    monkeypatch.setattr(mailer, "PORT", 465)
    _patch(monkeypatch, _Imap(['() "." "Archive"']))
    out = mailer.send("brent@bar.example", "following up", "Hi Brent")
    assert out["saved_to"] is None and "no Sent folder" in out["copy_error"]


def test_the_check_lists_folders_and_picks_sent(monkeypatch):
    _patch(monkeypatch, _Imap(['(\\HasNoChildren) "." "INBOX"', '(\\Sent) "." "Sent"']))
    out = mailer.check_sent_folder()
    assert out["ok"] and out["folder"] == "Sent" and len(out["folders"]) == 2
