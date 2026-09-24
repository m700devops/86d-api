"""The email drafter's master sheet and brief, pure.

Everything the drafting model may say about 86'd lives in `master_sheet()`,
and every fact in it was checked against the code: the order email carries
the restaurant and manager name (main.py's /orders/email), the trial is 30
days from sign-up with no card (checkout only opens once it lapses), a
bottle's distributor, price and par are set once (the product book). One
claim from the owner's own sample email is deliberately NOT here: "a unique
order number". The distributor email has none today — its subject is "Order
from {bar} — {date}" — and a bar that checks is a bar that stops trusting the
rest. Add it here the day the order email carries one.

The drafts used to be bland for three reasons, all fixed below: the rules
capped the body at four sentences and banned lists (which ruled out the
owner's own best email), the model knew almost nothing (no price, no trial,
no phone, and nothing logged about the bar unless it was a follow-up), and it
ran on the cheapest model. crm.py sends this through `_claude_json` on a
stronger one. Covered by test_pitch.py.
"""
import os
import re
from typing import Optional

OWNER_NAME = os.getenv("COMPANY_OWNER_NAME") or "Stephan"
OWNER_TITLE = os.getenv("COMPANY_OWNER_TITLE") or "Owner of 86'd"
OWNER_PHONE = os.getenv("COMPANY_PHONE") or "910-335-2760"
PRICE = os.getenv("COMPANY_PRICE") or "$29.99/month"
APP_URL = (os.getenv("COMPANY_APP_URL")
           or "https://apps.apple.com/us/app/86d-bar-inventory/id6798359825")
WEBSITE = os.getenv("COMPANY_WEBSITE") or "https://my86d.com"

# The owner's own email, sent as the example of what good looks like. Given
# to the model as a reference for substance and structure, not a template to
# copy word for word.
EXAMPLE_EMAIL = f"""Subject: Cut bar inventory to 15 minutes

Hi [Name],

I built 86'd, an app that turns bar inventory and ordering into a 10–15 minute task.

Here's how it works:

1. Open the app and point your camera at a bottle. AI identifies it instantly.
2. Enter your current count and move to the next bottle.
3. In the background, AI organizes everything by distributor.
4. Tap "Email." Orders go out to all your distributors at once, without ever opening your inbox.

Each order is sent with your restaurant's name and your bar manager's name. You set each product's distributor and price once, and the app remembers it from then on. Every order is saved, so you can look back at your ordering history anytime.

The first month is free, with no credit card required. Just download and go. After that, it's {PRICE}.

If you have any questions, call me directly at {OWNER_PHONE}.

Best,
{OWNER_NAME}
{OWNER_TITLE}"""


# States with NO tip credit: tipped staff earn the full minimum wage, so an
# hour spent counting bottles has a real, quotable labour cost. The strongest
# angle there (86d-leads' pitch-angle rule, extended to every such state). In
# tip-credit states the labour story is weaker: lead with time and accuracy.
NO_TIP_CREDIT = {"AK", "CA", "MN", "MT", "NV", "OR", "WA"}


def state_of(loc: Optional[str]) -> Optional[str]:
    """"Austin, TX" -> "TX"."""
    m = re.search(r",\s*([A-Z]{2})\s*$", (loc or "").strip())
    return m.group(1) if m else None


def state_angle(loc: Optional[str]) -> Optional[str]:
    st = state_of(loc)
    if not st:
        return None
    if st in NO_TIP_CREDIT:
        return (f"{st} has no tip credit: whoever counts is paid full minimum wage for it, so "
                "the hours the count takes are real money. Lead with the time and the labour.")
    return (f"{st} allows a tip credit, so the labour-cost story is weaker there. Lead with "
            "time back and orders that come in right.")


def master_sheet() -> str:
    """Everything the AI may say about 86'd, and how to sell it honestly.

    The FACTS sections are the only product facts any AI may state; every one
    was checked against the code (see the module docstring). The rest is how an
    owner would brief a new rep: who we're for, what hurts, what we can't do,
    how to answer the usual pushback, and what we ask for."""
    return f"""WHO IS WRITING
- {OWNER_NAME}, who built 86'd and owns it. Founder writing to a bar, not a sales team.
- Direct line: {OWNER_PHONE}. Sign-off: "{OWNER_NAME}" then "{OWNER_TITLE}".

WHAT 86'D IS
- An iPhone app (iOS only, no Android) for bar inventory and distributor ordering.
- Turns the weekly count and the orders into a 10-15 minute job, instead of a clipboard,
  a spreadsheet and an evening of emails.

HOW IT WORKS
1. Point the camera at a bottle. AI identifies it (name, brand, category).
2. Tap in the current count on a number pad, move to the next bottle.
3. The app sorts everything by distributor in the background.
4. Tap "Email": every distributor gets its order at once, sent by the app. Nobody has
   to open their inbox or write an order by hand.

DETAILS THAT ARE TRUE
- Every order goes out with the restaurant's name and the bar manager's name on it, and
  the bar gets a copy.
- Each bottle's distributor, price and par level are set once; the app remembers them,
  so the next count only asks for the number. The first count builds the book as you go:
  there's no setup day.
- Every order is saved: order history, spend by distributor, most-ordered items.
- Staff names can be recorded against a count ("who counted this"); no extra logins.
- Works across more than one bar on one account.
- Nothing to buy or install beyond the app: no scale, no scanner, no hardware.

PRICE
- First month free. No credit card needed to start: download and go.
- After that, {PRICE}. Cancel any time, from the app.

LINKS (the only URLs that exist; never invent another)
- App Store: {APP_URL}
- Website: {WEBSITE}

WHAT IT DOES NOT DO (never claim or imply otherwise)
- No Android version. Whoever counts needs an iPhone.
- It doesn't connect to a POS or read sales; the count is what's on the shelf.
- It doesn't measure how full a bottle is: the count is tapped in by hand.
- It doesn't order through distributor websites or portals: it emails the order to the
  rep or the distributor's order address, the way most bars already order.
- No customer numbers, testimonials, case studies or percentages exist to quote. Never
  say "bars like yours", "hundreds of bars" or any figure beyond the price and the
  10-15 minutes.

WHO IT'S FOR
- Independent bars and restaurants with a full bar: one to a few locations, buying from
  their own distributor reps. Not chains with a corporate purchasing team.
- The person who counts and orders: usually the owner, the GM or the bar manager — not
  whoever answers the phone. Ask for them by role if there's no name.
- Best fit: still counting on paper or a spreadsheet, and ordering by text, phone or email
  to each rep separately.

PAINS TO ASK ABOUT (as questions, never as statements about their bar)
- How long the weekly count takes, when it happens, and who does it.
- How orders go out today: texts and emails to each rep, typed up after the count.
- Where the prices live: old invoices, a spreadsheet, someone's head.
- Running out of a top seller on a Friday, or money sitting on the shelf from over-ordering.
- What happens to the process when the person who does it leaves.

HONEST ANSWERS TO THE USUAL PUSHBACK
- "We already have a system." Ask what it is and how long a count takes with it. If it
  works for them, say so and leave the door open. 86'd is for teams still counting by hand.
- "I don't have time." That's the point: the count and the orders take 10-15 minutes. Offer
  a five-minute call outside service, or just the link to try it on the next count.
- "What does it cost?" {PRICE} after the free first month; no card to start.
- "My staff use Android." It's iPhone only for now: whoever counts needs an iPhone.
- "I order through my rep." Nothing changes with the rep: the app emails them the order.
- "Send me something." Send the App Store link and one line on how it works, and ask when
  to follow up.

WHAT WE ASK FOR (one per email or call, never all three)
- Try it on their next count: download from the App Store, first month free, no card.
- A short call with {OWNER_NAME} to see it: {OWNER_PHONE}.
- The name of whoever counts and orders, and when they're in."""


STYLE = """HOW TO WRITE IT — founder to bar person, 2026, not a 2015 sales template

The human part comes first:
- Open with THEM, not us. The first line is about their bar, their night, or the last
  conversation — a real detail from WHAT WE KNOW below — then why you're writing. If we
  know nothing specific, open with the job itself: the count at the end of a shift, the
  clipboard, the orders typed up after close. Never a fake compliment, never "I hope this
  finds you well", never "I came across your bar".
- Sound like someone who has stood behind a bar at 1am counting bottles. Plain words,
  contractions, short paragraphs. Warm, direct, a little dry. Zero hype.
- If a person was spoken to, name them and what they said, the way you'd remind a friend.
  A personal detail from the log (a pet, a busy weekend, a new menu) earns one friendly
  line, never more.

The modern part:
- One idea per email: inventory and ordering in 10-15 minutes. Don't list every feature.
- The four-step "how it works" list is allowed and works well for a first email; keep it
  to those steps. Skip it in a short follow-up unless they asked how it works.
- Make trying it effortless: the free month, no card, download and go, and the App Store
  link on its own line.
- End with ONE easy next step. A low-pressure question they can answer in a word
  ("Worth a look before your next order day?", "Want me to walk you through it on a
  call?") or the direct line. Not both a meeting ask and a demo ask and a link ask.
- Subject: 2-6 words, specific, reads like a person typed it. Their bar's name or the
  outcome ("Rioja's Sunday count", "inventory in 15 minutes"). No clickbait, no Title Case,
  no "Quick question", no emoji.
- Mobile-length: they read this on a phone between deliveries. A first email about as long
  as the EXAMPLE or shorter; a follow-up half that.
- Optional P.S. only if there is a genuinely personal line to put in it.

Hard rules:
1. NEVER state a product fact, price, number or URL that is not in the MASTER SHEET, and
   never a fact about the venue that is not in WHAT WE KNOW. No invented customers,
   testimonials, percentages or "bars like yours saved X". Unsure? Leave it out.
2. Every person named in the log is someone the sender talked to at the bar. They did
   not tell anyone about 86'd; never say or imply they did.
3. Include the App Store link in every email unless the salesperson says not to.
4. Plain text only: no HTML, no markdown, no **bold**. Numbered steps as "1." lines.
5. Do exactly what the salesperson asked. Their instruction beats the defaults above."""


def lead_context(lead: dict, fact_lines: Optional[list] = None,
                 points: Optional[list] = None, log: str = "") -> str:
    """WHAT WE KNOW about the recipient: only what's on file, each fact with
    where it came from, so the model can personalise without inventing."""
    out = [f"Venue: {lead.get('name') or 'the bar'}"
           + (f", {lead['loc']}" if lead.get("loc") else "")]
    who = lead.get("contact") or lead.get("manager_name")
    if who:
        role = lead.get("manager_role") or ("the person we spoke to" if lead.get("contact")
                                            else "listed on their website")
        out.append(f"Contact: {who} ({role})")
    angle = state_angle(lead.get("loc"))
    if angle:
        out.append(f"Angle for this state: {angle}")
    if lead.get("opener"):
        out.append(f"From their own website: {lead['opener']}")
    for line in fact_lines or []:
        text, source = line.get("text"), line.get("source")
        if text:
            out.append(f"{text} (from {source})" if source else text)
    if points:
        out.append("Talking points written earlier from those facts: " + " / ".join(points))
    if log:
        out.append("What has happened so far (the sender's own log, oldest first):\n" + log)
    if len(out) == 1:
        out.append("Nothing else is known about them. Don't pretend otherwise.")
    return "\n".join(out)


def system_prompt(knowledge: str = "", winners: Optional[list] = None) -> str:
    """Everything that's the same for every email this hour: the master sheet,
    the owner's instructions and the playbook (`knowledge`), the owner's own
    example, emails of ours that got a reply, and the style guide. Sent as the
    system prompt and CACHED, so a redraft — or the next bar's draft — reads
    it at a tenth of the price. What's specific to one bar goes in the user
    message (`user_prompt`)."""
    parts = [f"""You write one sales email for {OWNER_NAME}, who owns 86'd, to send from his own
mailbox to a bar. First person, in his voice, signed as him.

=== MASTER SHEET (the only product facts you may use) ===
{master_sheet()}"""]
    if knowledge:
        parts.append(f"=== WHAT THE OWNER SAYS AND WHAT WE'VE LEARNED ===\n{knowledge}")
    parts.append(f"""=== AN EMAIL THE OWNER LIKES (match its substance and clarity, not its exact words;
be more personal than it where WHAT WE KNOW allows) ===
{EXAMPLE_EMAIL}""")
    if winners:
        shown = "\n\n---\n\n".join(f"Subject: {w['subject']}\n\n{w['body']}" for w in winners)
        parts.append("=== EMAILS OF OURS THAT GOT A REPLY (real and recent: learn from what "
                     "worked. They were to OTHER bars, so never reuse a venue, name or detail "
                     f"from them) ===\n{shown}")
    parts.append(f"=== {STYLE}")
    parts.append('Return a JSON object with "subject" and "body". The body is the whole email, '
                 "sign-off included.")
    return "\n\n".join(parts)


def user_prompt(context: str, ask: str) -> str:
    """The part that changes per draft: who it's to, and what to write."""
    return f"=== WHAT WE KNOW ABOUT THIS BAR ===\n{context}\n\n=== WHAT TO WRITE ===\n{ask}"


WINNER_CHARS = 2500


SCHEMA = {
    "type": "object",
    "properties": {"subject": {"type": "string"}, "body": {"type": "string"}},
    "required": ["subject", "body"],
    "additionalProperties": False,
}
