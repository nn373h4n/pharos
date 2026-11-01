# Evaluating the model output in Azerbaijani, Turkish, Russian and English

Every default model of Pharos (the cheap filter, the enrichment of articles, the "how it applies"
suggestion) should be chosen from this evaluation, not from a model's own claims. People who speak
the language rate what the models write; the tool collects, hides which model wrote what, counts
the marks and says which model clears the bar. **No automated judge counts toward the bar.**

The result holds only for the prompt versions printed in the report. Run it again when a prompt
changes, when you consider a new model, and before you rely on a local model.

## What you need

- A provider and its key in the worker's environment (`PHAROS_LLM_PROVIDER`, `PHAROS_LLM_API_KEY`),
  as in production. The `fake` provider works only to try the tool; its report says it is not an
  evaluation and gives no advice.
- Two native speakers for each language you care about. They should not be the people who wrote the
  prompts. Each rates every row of their sheet.
- Money. A real run calls the models for every article, language and model you name. The tool
  stops at `--max-cost-usd` (five dollars unless you change it); raise it only on purpose. Pass the
  real price of a model with `--price model=IN:OUT` (US dollars per million tokens) so the cap and
  the cost per 100 answers are true.

## Steps

All commands run in the worker's container (it holds the key):

```text
docker compose exec worker python -m pharos.evaluation <command> ...
```

1. **Choose the articles.** The best test uses the articles your people will really get:

   ```text
   export-corpus --professions accountant,payroll_specialist,hr_specialist,sap_consultant \
     --per-profession 30 --negatives 10 --out /work/corpus.json
   ```

   Open the file. Each entry has `relevant`: set it to `false` for every article that is **not**
   about practical use of AI at work. Every exported article starts as `null` (not yet judged), and
   a run refuses the file until each one is `true` or `false`. That label judges the cheap filter. To try the tool first, use the sample
   `docs/guides/evaluation-corpus.sample.json`.

2. **Run the candidates.** Name the models to compare at each stage:

   ```text
   run --corpus /work/corpus.json --dir /work/eval \
     --filter small-a,small-b --enrichment big-a,big-b --insight big-a,big-b \
     --languages az,tr,ru,en --max-cost-usd 20 --price big-a=3:15 --price big-b=2:10
   ```

   Every call goes through the product's own gateway with the product's own prompts and checks.
   A failed call is recorded as a failure; nothing is retried.

3. **Make the sheets.** One per language, blind (a random code instead of the model, shuffled):

   ```text
   sheet --dir /work/eval --corpus /work/corpus.json --language az
   ```

   Give the CSV to the raters. They need nothing else: **never give them `answers.jsonl`**, which
   shows the model of every code.

4. **Rate.** In a spreadsheet each rater fills their own copy: `rater` (any name they choose, up to
   40 characters), then for each row:

   | Column | Meaning |
   | --- | --- |
   | `fluency` | 1 to 5. Is it natural, correct language, as a colleague would write it? 1: broken or machine-like; 5: native. |
   | `accuracy` | 1 to 5. Is it true to the article, with nothing invented? 1: wrong or made up; 5: faithful. |
   | `usefulness` | 1 to 5. Would it help someone in this profession do something real? 1: empty; 5: concrete and actionable. |
   | `safe` | `yes` or `no`. Would you put it in front of a colleague? Say `no` for anything misleading, risky, rude, or a prompt that asks for confidential data. |
   | `comment` | Optional. It is never stored or printed by the tool. |

   Leave a row entirely blank to skip it. Save as CSV (UTF-8).

5. **Import.** All sheets at once; one mistake stops the whole import and names the row:

   ```text
   import --dir /work/eval az-anna.csv az-vusal.csv tr-aylin.csv tr-mehmet.csv ...
   ```

6. **Report.**

   ```text
   report --dir /work/eval --out azerbaijani-evaluation-2026-11-15.md
   ```

   The report holds numbers only: no article, no model output, no comment, no rater name (raters
   appear as R1, R2). It states the bar, then per stage which model clears it, and the cheapest one
   that does is recommended with the exact lines for the worker's `.env`. If no model clears the
   bar it says so and why; **do not lower the bar to get an answer**. Keep the report out of the
   repository: it names your professions, languages and raters' numbers, which are pilot details.

## The bar (the defaults, each a flag of `report`)

- mean at least 4 out of 5 on fluency, accuracy and usefulness in every profession and every
  language;
- at least 90% of ratings `safe`, and at least 90% of answers passing the product's own checks
  (a failed call counts as a failure);
- at least 30 articles rated in each profession and language, by at least 2 raters in each
  language;
- the cheap filter right at least 90% of the time against your `relevant` labels.

Two raters whose marks differ by 2 or more on average are flagged: their sheets measure different
things, so agree on the meaning of the marks and rate again before you trust the result.

## After the report

1. Put the recommended lines in the worker's `.env` and restart the worker.
2. Keep the report (numbers only) with the date, the provider and the prompt versions.
3. Repeat the evaluation when a prompt version changes (`PROMPT_VERSIONS`), when you change the
   model, or when you add a language.
4. A local model (M4-10) is claimed only after it passes this same bar.
