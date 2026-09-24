"""One bar, one lead: the duplicate that put Olde Town on the call list twice.

Quick-add created a fresh row for a bar the generator had already put on the
call list, and the generator never compared phone numbers, so "Olde Town
Tavern" (the map) and "Olde Town Tavern & Grill" (the call that was logged)
lived as two leads: the worked one in the CRM tab, the never-called copy on
the call list. `same_venue()` and `duplicate_folds()` are the pure rules;
`_reconcile_duplicate_leads()` applies them on every boot. `database` is
stubbed the same way test_leadgen.py stubs it.
"""
import sys
import types

if "database" not in sys.modules:
    stub = types.ModuleType("database")
    stub.get_db = lambda: None
    sys.modules["database"] = stub

from leadgen import (  # noqa: E402
    _reconcile_duplicate_leads, duplicate_folds, same_venue,
)


def test_a_name_with_extra_generic_words_is_the_same_venue():
    assert same_venue("Olde Town Tavern & Grill", "Olde Town Tavern")
    assert same_venue("The Barrel House", "Barrel House Bar")
    assert same_venue("Joe's Bar", "Joe's")


def test_two_bars_one_owner_runs_off_one_phone_are_not():
    assert not same_venue("Blue Room", "The Monkey Bar")


def test_an_all_generic_name_has_to_match_exactly():
    assert same_venue("The Tavern", "the tavern")
    assert not same_venue("The Tavern", "The Pub")


def _lead(id, name, phone, *, worked=False, source="leadgen", created="2026-09-18"):
    return {"id": id, "name": name, "phone": phone, "source": source,
            "status": "contacted" if worked else "new",
            "last_touch_at": "2026-09-22T01:00:00Z" if worked else None,
            "created_at": created}


def test_the_never_called_copy_folds_into_the_worked_lead():
    worked = _lead("QA", "Olde Town Tavern & Grill", "(720) 242-9667",
                   worked=True, source="manual", created="2026-09-22")
    copy = _lead("GEN", "Olde Town Tavern", "+1 720.242.9667")
    assert duplicate_folds([copy, worked]) == [("QA", "GEN")]


def test_a_shared_phone_with_a_different_name_is_left_alone():
    assert duplicate_folds([_lead("A", "Blue Room", "720-242-9667", worked=True),
                            _lead("B", "The Monkey Bar", "720-242-9667")]) == []


def test_the_operators_own_rows_and_worked_rows_are_never_folded():
    manual = _lead("M", "Olde Town Tavern", "720-242-9667", source="manual")
    worked = _lead("W", "Olde Town Tavern", "720-242-9667", worked=True, created="2026-09-01")
    assert duplicate_folds([manual, worked]) == []
    both_worked = [_lead("W1", "Olde Town Tavern", "720-242-9667", worked=True),
                   _lead("W2", "Olde Town Tavern", "720-242-9667", worked=True)]
    assert duplicate_folds(both_worked) == []


def test_two_never_called_copies_keep_the_older_one():
    older = _lead("OLD", "Olde Town Tavern", "720-242-9667", created="2026-09-10")
    newer = _lead("NEW", "Olde Town Tavern", "720-242-9667", created="2026-09-20")
    assert duplicate_folds([newer, older]) == [("OLD", "NEW")]


class _Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.seen = []

    def execute(self, sql, params=()):
        self.seen.append((" ".join(sql.split()), params))

    def fetchall(self):
        return self.rows


def test_reconcile_moves_everything_to_the_keeper_before_deleting_the_copy():
    cur = _Cursor([_lead("QA", "Olde Town Tavern & Grill", "720-242-9667",
                         worked=True, source="manual"),
                   _lead("GEN", "Olde Town Tavern", "720-242-9667")])
    assert _reconcile_duplicate_leads(cur) == 1
    writes = [(s.split()[0] + " " + s.split()[1] + " " + s.split()[2], p)
              for s, p in cur.seen[1:]]
    # Fill the keeper's gaps, re-point the candidate and any queued email,
    # and only then delete the copy — never the keeper.
    assert [w for w, _ in writes] == ["UPDATE crm_leads k", "UPDATE crm_lead_candidates SET",
                                      "UPDATE crm_scheduled_emails SET", "DELETE FROM crm_leads"]
    assert writes[0][1] == ("QA", "GEN")
    assert writes[3][1] == ("GEN",)
