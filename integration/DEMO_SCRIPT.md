# Demo script (voice helpdesk, AKGEC)

Everything below passed through the real Brain with the published KB (kb_version a63f6a4e53f7, 20 entries) on the typed path: see
`integration/TOP40_RESULTS.md`. Say the sentences close to as written; the Brain answers from the reviewed entries only.

## Before you start (2 minutes)
1. `integration\run_all.ps1 -Python <venv python> -LlmMode live` (starts ops :8100, runtime :8000 with the real Brain, dashboard :5173).
2. Open the dashboard (admin@demo) and keep it on "Active calls" / "Tickets" in a second window.
3. **Warm-up:** say or type one short question first ("hello"). The first live LLM call after a restart can take 2-3 s; the demo questions below are mostly FAQ fast-path (well under 1 s).
4. Pick the path:
   - **Agora call** (real-time voice agent): http://localhost:8000/call, headphones on (the agent must not hear itself), allow the mic.
   - **Browser voice** (Chrome/Edge speech): http://localhost:8000/voice/ (Speak in = hi-IN for Hindi/Hinglish, en-IN for English). Needs no Agora minutes.
5. Voice note: the managed Agora voice is `English_captivating_female1` (the only managed voice verified audible). Hindi is spoken with an English accent; say so if asked.

## Questions that pass best (all three languages)
| Topic | English | Hinglish / Hindi |
|---|---|---|
| B.Tech fee | How much is the B.Tech first year fee? | B.Tech ki fees kitni lagti hai |
| Pay fees | How do I pay my fees online? | fees online kaise bharni hai |
| Hostel fee | What is the hostel fee for a B.Tech student? | hostel ki fees kitni hai |
| Scholarship | What scholarships does the college give? | scholarship kitne rank tak milti hai |
| Refund | What is the fee refund policy if I cancel my admission? | admission cancel karne par paise wapas milenge kya |
| Datesheet | Where can I see the exam datesheet? | date sheet kahan milegi |
| Result | Where can I check my semester result? | result kahan dekhna hai |
| Calendar | Where can I find the academic calendar? | academic calendar kahan milega |
| Certificates | How do I get my migration certificate? | character certificate kaise banwayen |
| Lost ID | I lost my ID card, what should I do? | duplicate ID card kaise banega |
| Bus routes | Which areas do the college buses cover? | college bus kin routes pe chalti hai |
| Bus pass | How do I get a bus pass? | bus pass kaise banega |
| Library | What are the library timings? | library ka time kya hai |
| Library fine | How many books can I borrow from the library? | late book return karne par kitna fine lagta hai |
| Canteen | What are the canteen timings? | canteen kab khulta hai |
| Sports | What sports facilities are there in the college? | gym hai kya hostel mein |
| Placement | How do I contact the placement cell? | placement cell ka contact kya hai |
| Address | What is the college address? | college kaise pahunchen |
| Doctor | Is there a doctor or hospital on campus? | (English or Hindi only, see below) |

## Flows to show on the dashboard
- **Unknown question** (honest "no verified info", offers a ticket): "What is the date of the annual cultural fest?" -> Brain says it has no verified information, offers a ticket, creates it after "yes, my number is 9876543210"; shows under Tickets and in Knowledge gaps.
- **Exam within the hour -> P1 + transfer** (English works every time): "My data structures exam starts in 15 minutes and I cannot download my admit card, roll number 2400271549999". Hinglish: "mera exam 15 minute mein hai aur hall ticket nahi mil raha" then the paper name when asked. Dashboard: P1 ticket (R-EXAM-60), staff alert.
- **Emergency** (script first, then ticket + alert + transfer): "my friend fainted in the hostel, please send help" / "mere dost ko chakkar aa gaya aur wo behosh ho gaya".
- **Human request:** "I want to speak to a person" -> ticket and callback or transfer depending on office hours.
- **Complaint with readback:** "the wifi in my hostel is not working, I want to file a complaint" -> block/room, mobile number, readback, "yes that is correct" -> ticket + SMS.
- After **End call** the call shows in "Answered by AI" when it was resolved without a ticket.

## Do NOT use on stage (they trigger the emergency path by design)
- "ragging" (Q38: "How do I report ragging?" / "ragging ki complaint kahan karni hai") -> emergency script and transfer, not the FAQ answer.
- "ambulance" in Hinglish ("college mein ambulance hai kya") -> emergency path. Use "Is there a doctor or hospital on campus?" instead.
- Words from the safety lexicon in casual use: fire, accident, "mar jaunga", "fail hua toh ...", blood, suicide.
- Also avoid: bonafide certificate, mess timing, hostel gate time, bus route numbers: not in the published KB, they become tickets.

## If something goes wrong
- Silent agent on /call: stop the call, check `GET .../agents?state=2` is empty (no stranded agent), restart the stack, retry once.
- Garbled Hindi understanding: set `ASR_LANGUAGE=multi` in integration/.env and restart.
- Fallback line "Sorry, we are having a technical problem" + ticket: the LLM timed out (3 s); retry the question, it usually answers from the FAQ path.
