# Decisions (top-40 KB task)

1. **Ids.** New entries use the 2xx range (FEE-201 ...) because Member A's file already uses the 1xx range and the brain's curated file uses 0xx. No id clashes with either, with `contracts/kb/sample_kb.jsonl`, or with the entries in the local ops database (checked in VALIDATION.md).
2. **Synthetic corpus text is not a source.** `domains/campus_akgec/corpus/*.md` marks some sections `provenance: synthetic` (late fee, mess, gate times, leave, visitors, bonafide, transport, attendance, admit card, carry-over, re-evaluation). They were treated as missing data, not as facts.
3. **Member A entries with a document name instead of a URL** were not used and not imported; they are listed in VERIFY_WITH_A.md for Member A to confirm.
4. **Contacts.** Only the college helpline (8744052891 to 93), official college email addresses published on official pages (director@, tpo@, the anti-ragging convener) and the Dean's name are used. Personal mobile numbers on the grievance page and the placeholder numbers in departments.yaml are not used.
5. **Conflict on library opening time** (8:15 vs 8:30): the answer says "about 8:15 to 8:30"; flagged in MISSING_DATA.md section C.
6. **Hindi answers** are written in Devanagari; English and Hinglish are in the question variants only, because the bot answers in the caller's language from answer_en/answer_hi.
7. **Partial answers** state what is known and, where useful, send the caller to the official helpline instead of guessing (TRN-201, TRN-202, FAC-205).
8. **No `valid_until`.** Sources give sessions (2026-27) rather than dates.
9. **Bank account number** on the fee page is not put in the KB (not suitable to read aloud).
10. **Department for hostel and bus fees** is `accounts` / `transport` respectively where the question is about money or transport, so a transfer goes to the office that can act.
