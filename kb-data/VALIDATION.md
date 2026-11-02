# Validation: top-40 drafts

File: `kb-data/top40_kb.jsonl`. Entries: 20.

| Check | Result |
|---|---|
| Every line parses as `resolvix_contracts.KBEntry` | PASS |
| Ids unique, and not used in sample_kb.jsonl, Member A's file or brain/data/kb.jsonl | PASS |
| Departments exist in departments.yaml (accounts, admissions, emergency, exam_cell, helpdesk, hostel, transport) | PASS |
| 5 to 8 variants, with Devanagari and Latin forms | PASS |
| answer_hi in Devanagari, source_ref is a URL, status draft | PASS |
| No personal data (roll numbers, marks, dues, personal mobiles, Aadhaar-like numbers; the helpline 8744052891 is allowed) | PASS |
| Injection scan (`ignore (all|previous) instructions|system prompt|you are now|disregard|act as`) | PASS |
| No list/table formatting in spoken answers | PASS |

## Entries

| id | department | variants | sentences (en) | chars (en) |
|---|---|---|---|---|
| FEE-201 | accounts | 8 | 2 | 267 |
| FEE-202 | accounts | 8 | 2 | 309 |
| FEE-203 | accounts | 8 | 2 | 363 |
| FEE-204 | accounts | 8 | 2 | 375 |
| FEE-205 | accounts | 8 | 2 | 360 |
| EXM-201 | exam_cell | 8 | 2 | 221 |
| EXM-202 | exam_cell | 8 | 2 | 195 |
| ACD-201 | helpdesk | 8 | 2 | 267 |
| ACD-202 | helpdesk | 8 | 2 | 358 |
| ACD-203 | helpdesk | 8 | 2 | 246 |
| TRN-201 | transport | 8 | 2 | 201 |
| TRN-202 | transport | 8 | 2 | 199 |
| FAC-201 | helpdesk | 8 | 2 | 248 |
| FAC-202 | helpdesk | 8 | 2 | 299 |
| FAC-203 | helpdesk | 8 | 4 | 226 |
| FAC-204 | helpdesk | 8 | 2 | 166 |
| FAC-205 | helpdesk | 8 | 2 | 160 |
| GEN-201 | helpdesk | 8 | 2 | 326 |
| PLC-201 | helpdesk | 8 | 3 | 177 |
| GEN-202 | helpdesk | 8 | 2 | 181 |

## Problems

None.

## Import into local ops (http://localhost:8100)

HTTP 200: `{"imported": 20, "flagged": 0, "errors": []}`

