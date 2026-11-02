# Member A entries to confirm before approval

Source file: `kb-data/akgec_from_A.jsonl` (copied from `origin/data:contracts/kb/akgec_cag_kb.jsonl`, 57 draft entries). The `data` branch was not merged.
These entries are NOT imported into ops. They are listed first in the review gate. For each one Member A must give the real document (file name, issuing office, date) or a URL, or withdraw the entry.

## 1. UNVERIFIED: source_ref is not a real URL (24 entries)

| id | Facts stated (exact) | Cited source |
|---|---|---|
| GEN-105 | The administrative office is open Monday to Friday from 9:00 AM to 5:00 PM, and on Saturday from 9:00 AM to 1:00 PM. It remains closed on Sundays and official holidays. | Office Directory section 1 |
| GEN-119 | A minimum of 75% attendance across all scheduled lectures and practical sessions is strictly required by AKTU and college regulations to appear in end-semester examinations. | Academic Rulebook section 4.1 |
| GEN-120 | AKGEC enforces a strict zero-tolerance anti-ragging policy with 24x7 security, comprehensive CCTV surveillance, and a dedicated Anti-Ragging Committee and Squad. | Student Welfare Manual section 1 |
| ADM-102 | Candidates must have passed 10+2 with Physics and Mathematics as compulsory subjects alongside Chemistry/Biotechnology/Computer Science, securing minimum 45% marks (40% for SC/ST). | UPTAC Information Brochure 2025-26 |
| ADM-103 | Candidates holding a 3-year Engineering Diploma or B.Sc degree with Mathematics are eligible for lateral entry admission directly into the 2nd year (3rd semester) via UPTAC counselling. | AKTU Lateral Entry Ordinance |
| ADM-108 | Eligible SC, ST, OBC, General, and EWS students can apply for the UP Government Post-Matric Scholarship via scholarship.up.gov.in and submit verified printouts to the College Registrar office. | Registrar Notice Board section 3 |
| FEE-104 | A late fine of Rs 100 per day applies on semester fee dues after the notified deadline, up to a maximum period of 15 days. | Fee Notice 2026-27 p.2 |
| FEE-108 | Fee receipts are generated digitally on the college ERP portal under Fee Payment within one working day of payment clearance, suitable for tax and education loan claims. | ERP Fee Portal Guide section 2 |
| EXM-101 | Odd semester theory examinations are typically conducted in December-January, and even semester examinations in May-June, as notified on the official AKTU academic calendar. | Academic Calendar 2026-27 p.3 |
| EXM-102 | The college conducts two internal sessional exams and one comprehensive Pre-University Test (PUT) per semester to evaluate continuous internal assessment marks. | Internal Assessment Manual section 3 |
| EXM-103 | Admit cards are published on the AKTU ERP student portal approximately three days prior to the commencement of the first theory examination. | Exam Notice 14 p.1 |
| EXM-104 | The daily exam seating plan is displayed on notice boards of respective academic blocks and published on the college ERP portal one day before each exam. | Exam Notice 14 p.2 |
| EXM-105 | Students can apply for challenge evaluation or scrutiny online through the AKTU ERP portal within 7 to 15 days of result declaration upon paying the prescribed university fee. | AKTU Examination Rules rule 9.2 |
| EXM-106 | Carry-over examinations (back papers) for odd semesters are held alongside regular odd semester exams, and even semester carry-overs alongside regular even semester exams. | AKTU Ordinance Clause 8 |
| EXM-107 | Provisional degree certificates and consolidated grade cards are issued by the College Registrar office after final semester result clearance and submission of the no-dues slip. | Registrar Section 2 |
| HST-104 | Hostel gates close strictly at 10:00 PM on weekdays (Monday to Friday) and at 11:00 PM on weekends (Saturday and Sunday). | Hostel Rules 2026 p.1 |
| HST-105 | Mess timings are: Breakfast from 7:30 AM to 9:00 AM, Lunch from 12:30 PM to 2:00 PM, and Dinner from 7:30 PM to 9:00 PM. | Hostel Rules 2026 p.2 |
| HST-107 | Room change requests must be submitted using Form H-4 at the Chief Warden office within 7 days of room allotment, subject to mutual agreement and room availability. | Hostel Rules 2026 p.3 |
| HST-108 | Hostelers must apply for night leave through the online warden portal with verified parental consent via registered contact at least 24 hours prior to departure. | Hostel Outpass Guideline section 2 |
| TRN-101 | AKGEC runs a fleet of college buses covering extensive routes across Delhi, Noida, Greater Noida, Ghaziabad, Meerut, Anand Vihar, and Dilshad Garden for day scholars. | Transport Desk Circular 2026 |
| TRN-102 | Students apply for bus passes through the Transport Coordinator desk by submitting the route registration form and paying transport fees for the semester or academic year. | Transport Desk Office Guide section 1 |
| TRN-103 | College buses reach the campus by 8:30 AM in the morning and depart from the campus bus bay at 5:10 PM on all working days. | Transport Timetable 2026-27 |
| EMG-101 | AKGEC maintains an on-campus medical dispensary with visiting medical officers, nursing staff, and a dedicated 24x7 ambulance tied to Dr. R.S.G. (Indo-German) Hospital. | Health Centre Manual section 1 |
| EMG-102 | For immediate campus emergencies, contact the 24x7 Campus Security Control Room at the Main Gate or dial police helpline 112 for external emergency response. | Security SOP section 1 |

## 2. URL cited, but the stated fact is not on the cited page (as saved in the local crawl): please re-check

| id | Problem |
|---|---|
| GEN-101 | 'established ... by the Indian School of Computer Science': the founder claim is not on any crawled AKGEC page (the pages only say established in 1998). |
| GEN-104 | '40-acre campus' is not on any crawled AKGEC page. |
| GEN-117 | The list of sports grounds and the 'Spardha' fest are not on the crawled pages (the hostel page only lists gymnasium and indoor/outdoor sports). |
| FEE-101 | 'college ERP fee portal (net banking, debit/credit cards)' is not on the fee page; the page lists draft/pay order, NEFT/RTGS and the Online Fee Payment (Kotak) link. |
| FEE-102 | Says roll number, branch and 'accounts counter or registered post'; the page says name and mobile number on the back, by post or in person. |
| FEE-107 | 'AKTU ERP student portal' and 'digital consortium fee' wording differ from the page (examination fee and digital library fee paid to the university). |
| HST-106 | 'high-speed Wi-Fi' and 'TV lounges' are not exact; the hostel page says Wi-Fi internet and a TV room. |

## 3. Departments

All departments used in Member A's file exist in `contracts/config/departments.yaml` (`helpdesk`, `admissions`, `accounts`, `exam_cell`, `hostel`, `transport`, `emergency`). No mapping was needed.

## 4. Entries with a real URL that agree with the sources

GEN-102, GEN-103, GEN-106 to GEN-116, GEN-118, ADM-101, ADM-104 to ADM-107, FEE-103, FEE-105, FEE-106, FEE-109, HST-101 to HST-103: consistent with the official pages checked, not part of the top-40, left for the normal review.
