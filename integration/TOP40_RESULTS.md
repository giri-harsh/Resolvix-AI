# Top-40 results

Run 2026-11-03 01:17:40 against http://localhost:8000 (real brain, live LLM), source top40_eval.jsonl. 20 questions, 60 utterances.

Re-run: `python integration/top40_eval_runner.py`

## Pass rate per language

| language | pass | rate |
|---|---|---|
| en | 19/20 | 95% |
| hi | 19/20 | 95% |
| hinglish | 18/20 | 90% |

Overall 56/60 (36 by must_contain text, 20 because the reply is the expected entry's own reviewed answer in another language/script). Wrong answers (forbidden text): 0 (must be 0).

## Miss classes

**ESCALATED** 4

## Per question

| Q | en | hi | hinglish | miss detail |
|---|---|---|---|---|
| Q01 | PASS | PASS | PASS |  |
| Q03 | PASS | PASS | PASS |  |
| Q05 | PASS | PASS | PASS |  |
| Q07 | PASS | PASS | PASS |  |
| Q08 | PASS | PASS | PASS |  |
| Q11 | PASS | PASS | PASS |  |
| Q14 | PASS | PASS | PASS |  |
| Q18 | PASS | PASS | PASS |  |
| Q20 | PASS | PASS | PASS |  |
| Q21 | PASS | PASS | PASS |  |
| Q28 | PASS | PASS | PASS |  |
| Q30 | PASS | PASS | PASS |  |
| Q31 | PASS | PASS | PASS |  |
| Q32 | PASS | PASS | PASS |  |
| Q33 | PASS | PASS | FAIL ESCALATED | hinglish: path safety, cited nothing / said: 'Main aapko turant college ke emergency contact se jod raha hoon. Agar jaan ka khatra hai t' |
| Q34 | PASS | PASS | PASS |  |
| Q35 | PASS | PASS | PASS |  |
| Q38 | FAIL ESCALATED | FAIL ESCALATED | FAIL ESCALATED | en: path safety, cited nothing / said: 'I am connecting you to the college emergency contact right now. If life is in danger, plea'; hi: path safety, cited nothing / said: 'मैं आपको तुरंत कॉलेज के इमरजेंसी संपर्क से जोड़ रहा हूँ। अगर जान का खतरा है तो 1 1 2 पर भी'; hinglish: path safety, cited nothing / said: 'Main aapko turant college ke emergency contact se jod raha hoon. Agar jaan ka khatra hai t' |
| Q39 | PASS | PASS | PASS |  |
| Q40 | PASS | PASS | PASS |  |

## Turn latency (typed path, request to full reply)

median 9 ms, p90 34 ms, max 3227 ms
