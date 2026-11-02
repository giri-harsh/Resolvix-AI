# Top-40 core questions: plan and coverage

Status: COVERED = one entry answers it fully from an official source; PARTIAL = an entry answers part of it, the rest is in MISSING_DATA.md; MISSING = no official source found, no entry written (the bot should decline or hand over).
Departments are keys of `contracts/config/departments.yaml`. Planned ids for MISSING rows are reserved for when data arrives.

| Q | Category | Question | Status | KB entry id | Department | Source |
|---|---|---|---|---|---|---|
| Q01 | Fees | B.Tech fee per year/semester | PARTIAL | FEE-201 | accounts | https://akgec.ac.in/fee-structure-for-new-students2026-27 |
| Q02 | Fees | Last date to pay semester fee | MISSING | (FEE-206 planned) | accounts | none |
| Q03 | Fees | How to pay fees online (portal, modes) | COVERED | FEE-202 | accounts | https://akgec.ac.in/fee-structure-for-new-students2026-27 |
| Q04 | Fees | Late fee fine | MISSING | (FEE-207 planned) | accounts | none |
| Q05 | Fees | Hostel fee | COVERED | FEE-203 | accounts | https://akgec.ac.in/fee-structure-for-new-students2026-27 |
| Q06 | Fees | Bus/transport fee | MISSING | (TRN-203 planned) | transport | none |
| Q07 | Fees | Scholarship schemes and how to apply | PARTIAL | FEE-204 | accounts | https://www.akgec.ac.in/scholarship/ |
| Q08 | Fees | Fee refund policy | COVERED | FEE-205 | accounts | https://www.akgec.ac.in/fee-refund-norms/ |
| Q09 | Exams | When semester exams start | MISSING | (EXM-203 planned) | exam_cell | none |
| Q10 | Exams | Admit card where/when | MISSING | (EXM-204 planned) | exam_cell | none |
| Q11 | Exams | Date sheet where published | COVERED | EXM-201 | exam_cell | https://akgec.ac.in/academics/academic-calendar |
| Q12 | Exams | Backlog/supplementary exam process and dates | MISSING | (EXM-205 planned) | exam_cell | none |
| Q13 | Exams | Exam form last date | MISSING | (EXM-206 planned) | exam_cell | none |
| Q14 | Exams | Result when/where | PARTIAL | EXM-202 | exam_cell | https://akgec.ac.in/academics/academic-calendar |
| Q15 | Exams | Re-evaluation process | MISSING | (EXM-207 planned) | exam_cell | none |
| Q16 | Exams | Minimum attendance for exams | MISSING | (ACD-204 planned) | exam_cell | none |
| Q17 | Academic | College timings | MISSING | (ACD-205 planned) | helpdesk | none |
| Q18 | Academic | Academic calendar (semester start/end, holidays) | PARTIAL | ACD-201 | helpdesk | https://akgec.ac.in/academics/academic-calendar |
| Q19 | Academic | Bonafide certificate | MISSING | (ACD-206 planned) | helpdesk | none |
| Q20 | Academic | Migration/character certificate or transcript | PARTIAL | ACD-202 | helpdesk | https://www.akgec.ac.in/academics/issuance-of-certificates-for-passout-students/ |
| Q21 | Academic | Lost ID card | COVERED | ACD-203 | helpdesk | https://www.akgec.ac.in/about/infrastructure-visual-tour/library/ |
| Q22 | Academic | HOD / department office location | MISSING | (ACD-207 planned) | helpdesk | none |
| Q23 | Hostel and mess | Mess timings | MISSING | (HST-201 planned) | hostel | none |
| Q24 | Hostel and mess | Hostel gate closing / in-time | MISSING | (HST-202 planned) | hostel | none |
| Q25 | Hostel and mess | Hostel leave / night-out permission | MISSING | (HST-203 planned) | hostel | none |
| Q26 | Hostel and mess | Hostel Wi-Fi or room complaint contact | MISSING | (HST-204 planned) | hostel | none |
| Q27 | Hostel and mess | Hostel guest/visitor rules | MISSING | (HST-205 planned) | hostel | none |
| Q28 | Transport | Bus routes covered | PARTIAL | TRN-201 | transport | https://www.akgec.ac.in/about/infrastructure-visual-tour/support-facilities/ |
| Q29 | Transport | Bus timings | MISSING | (TRN-204 planned) | transport | none |
| Q30 | Transport | Bus pass process | PARTIAL | TRN-202 | transport | https://akgec.ac.in/uptac |
| Q31 | Facilities | Library timings | COVERED | FAC-201 | helpdesk | https://www.akgec.ac.in/about/infrastructure-visual-tour/library/ |
| Q32 | Facilities | Library book limit and late fine | COVERED | FAC-202 | helpdesk | https://www.akgec.ac.in/about/infrastructure-visual-tour/library/ |
| Q33 | Facilities | Medical room / doctor timings / ambulance | PARTIAL | FAC-203 | helpdesk | https://www.akgec.ac.in/about/infrastructure-visual-tour/support-facilities/ |
| Q34 | Facilities | Canteen timings | COVERED | FAC-204 | helpdesk | https://www.akgec.ac.in/about/infrastructure-visual-tour/college-canteen/ |
| Q35 | Facilities | Sports facilities and timings | PARTIAL | FAC-205 | helpdesk | https://www.akgec.ac.in/about/infrastructure-visual-tour/hostel/ |
| Q36 | Admin and contacts | Admin/accounts office timings | MISSING | (GEN-203 planned) | helpdesk | none |
| Q37 | Admin and contacts | Exam cell contact | MISSING | (EXM-208 planned) | exam_cell | none |
| Q38 | Admin and contacts | Anti-ragging helpline/committee | PARTIAL | GEN-201 | helpdesk | https://www.akgec.ac.in/online-reporting-of-ragging-cases/ |
| Q39 | Admin and contacts | Training and placement cell contact/process | PARTIAL | PLC-201 | helpdesk | https://www.akgec.ac.in/placements/tp-department/ |
| Q40 | Admin and contacts | College address / how to reach | COVERED | GEN-202 | helpdesk | https://www.akgec.ac.in/ |

**Totals: COVERED 9, PARTIAL 11, MISSING 20.** 20 entries written.

## Sources used
- Official AKGEC pages as saved in `domains/campus_akgec/corpus/*.md` (only sections marked `provenance: sourced`) and the crawl in `research/akg_data.csv` (library, canteen, support facilities, scholarship, refund, ragging, T&P, grievance pages).
- Not used as facts: every section marked `provenance: synthetic` in that corpus, and Member A's entries whose `source_ref` is not a URL (see VERIFY_WITH_A.md).
