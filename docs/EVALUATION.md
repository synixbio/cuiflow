# Evaluation

Phase 1 of the design plan (DESIGN_PLAN §11): a harness that scores every engine mode on
the same reference set, baseline scores from two tool-made references, and scores on a synthetic
gold set.

**Neither tool-made reference is a gold standard.** Both are the output of a Java tool that one
of the engines reproduces, so each favours "its" engine. They are enough to test the harness and
to check the ensemble gates; they are not enough to claim accuracy. That needs a human-annotated
set (see [What a gold set needs](#what-a-gold-set-needs)).

**Every report under `docs/eval/` was produced by cuiflow 0.1.0.** Rerun a report to get
figures for later code.

**Where the gold set lives.** The gold set, the annotation guidelines, the annotators'
worksheets and the adjudication records are kept outside this repository. The reports under
`docs/eval/` stay here. Commands below write `<gold.jsonl>` for your copy of the file.

## How to run it

```powershell
$env:CUIFLOW_MMLITE_INDEX="data/ivf"
$env:CUIFLOW_UMLSMATCH_DB="data/umls_sno_rx.sqlite"
$modes="single:mmlite,single:umlsmatch,ensemble:union,ensemble:consensus"

# Java cTAKES on umlsmatch's 20 synthetic notes (with cTAKES' assertion labels)
cuiflow evaluate --format ctakes-silver --reference ../umlsmatch/free_texts/json/silver.jsonl `
    --modes $modes --out docs/eval/<date>-ctakes-silver

# Java MetaMapLite 3.6.2rc8 on mmlite's 12-document parity corpus (concepts only)
cuiflow evaluate --format mmlite-fixtures --reference ../mmlite/tests/parity/fixtures `
    --corpus ../mmlite/tests/parity/corpus --modes $modes --out docs/eval/<date>-mmlite-fixtures
```

Each engine loads once and is shared across the modes. `--scope` sets the semantic groups scored
on **both** sides (default: cTAKES' five major groups: DISORDER, FINDING, DRUG, PROCEDURE,
ANATOMY). This matters because umlsmatch's dictionary covers cTAKES' 49 semantic types and
mmlite covers all of them; scoring outside a shared scope measures scope, not extraction.

**Concepts** are scored three ways:
- **Overlap** (the headline): same CUI, spans overlapping after edge punctuation is trimmed,
  matched one-to-one. The matching is maximal: exact-span pairs first, then as many more as the
  overlaps allow (a greedy pass could leave a mention unmatched when two compete for one). On
  the gold set this changes no score.
- **Exact:** same CUI and offsets.
- **CUI-set:** CUIs per document, ignoring position.

**Assertions** are scored on overlap-matched pairs where the reference has a label.
`coverage` is the share of those that the mode also assessed. These figures are agreement with
the reference's labels, not accuracy.

## Baseline

UMLS 2026AA indexes for both engines; scope = the five major groups. Full reports:
[docs/eval/](eval/).

### Against Java cTAKES (20 synthetic notes; favours umlsmatch)

| mode | ref | pred | overlap P / R / F1 | CUI-set F1 |
|---|---:|---:|---|---:|
| `single:mmlite` | 1766 | 4617 | 0.204 / 0.533 / 0.295 | 0.326 |
| `single:umlsmatch` | 1766 | 2174 | 0.671 / 0.826 / **0.741** | 0.757 |
| `ensemble:union` | 1766 | 5505 | 0.278 / 0.865 / 0.420 | 0.430 |
| `ensemble:consensus` | 1766 | 1287 | 0.678 / 0.494 / 0.572 | 0.615 |

### Against Java MetaMapLite (12 documents; favours mmlite)

| mode | ref | pred | overlap P / R / F1 | CUI-set F1 |
|---|---:|---:|---|---:|
| `single:mmlite` | 495 | 490 | 0.984 / 0.974 / **0.979** | 0.975 |
| `single:umlsmatch` | 495 | 387 | 0.473 / 0.370 / 0.415 | 0.432 |
| `ensemble:union` | 495 | 696 | 0.697 / 0.980 / 0.814 | 0.826 |
| `ensemble:consensus` | 495 | 184 | 0.978 / 0.364 / 0.530 | 0.538 |

### Negation agreement with cTAKES' labels

| mode | positives | P / R / F1 |
|---|---:|---|
| `single:mmlite` (NegEx) | 79 | 0.829 / 0.861 / 0.845 |
| `single:umlsmatch` | 97 | 0.519 / 0.990 / 0.681 |
| `ensemble:consensus` | 76 | 0.630 / 0.987 / 0.769 |

Do not read this as "mmlite negates better". umlsmatch's adjudication found cTAKES marks
review-of-systems negatives ("Negative for chills, fever…") as affirmed; umlsmatch negates them,
and is scored as wrong for it. Against adjudicated verdicts umlsmatch's negation scores F1 0.870
(umlsmatch `docs/ADJUDICATION_RESULTS.md`). The other attributes have too few reference
positives here (3 to 17) to carry a figure.

## What the baseline shows

1. **The harness reproduces the published figures.** umlsmatch's CUI-set F1 against cTAKES is
   0.757 here, and umlsmatch's README reports 0.756 for the same dictionary. mmlite's is 0.979
   against Java MetaMapLite, near the published 0.969 (the scope differs: five groups here, all
   types there).
2. **The engines are complementary, and the references cannot say which is better.** Each scores
   high on its own reference and low on the other's. Most of that gap is dictionary scope and
   segmentation convention, not error.
3. **Neither ensemble mode passes its gate** (DESIGN_PLAN §11, Phase 3: beat both single engines
   on the metric it targets without losing more than a set margin of F1):
   - `union` targets recall and does raise it on both references (0.865 vs 0.826; 0.980 vs
     0.974), but loses 0.32 and 0.17 F1 against the better single engine. **Fails.**
   - `consensus` targets precision. It is barely above umlsmatch on the cTAKES set (0.678 vs
     0.671) and below mmlite on the MetaMapLite set (0.978 vs 0.984), with recall roughly
     halved. **Fails.**

   The defaults therefore stay single-engine. Union's precision loss is mostly mmlite's broad
   scope (e.g. several CUIs for "Patient"). A union restricted to mentions umlsmatch's semantic
   types cover, or weighted by `found_by`, is the next thing worth measuring, **on a gold set**.
4. **These corpora are development data.** Both engines were tuned on them, so all figures are
   optimistic. No figure here is a held-out result.

## Hybrid mode (`ensemble:hybrid_staged`)

Measured with umlsmatch 0.1.0 plus the `assess()` change later released in umlsmatch 0.2.0. Full reports:
[docs/eval/](eval/) (`*-hybrid-*`).

- **Concepts are mmlite's, exactly:** identical scores to `single:mmlite` on both references
  (overlap F1 0.295 against cTAKES, 0.979 against MetaMapLite). This confirms the wiring.
- **Assertions on the same 942 matched mentions** (cTAKES set):

  | attribute | `single:mmlite` (NegEx) | `ensemble:hybrid_staged` (umlsmatch rules) |
  |---|---|---|
  | negated | coverage 1.0; P 0.829 / R 0.861 / F1 0.845 | coverage 1.0; P 0.623 / R 0.962 / F1 0.756 |
  | history_of | coverage 0 (never assessed) | coverage 1.0; 6 positives, P 0.500 / R 1.000 |
  | subject, uncertain | coverage 0 | coverage 1.0 (0 and 8 positives) |

  Every mmlite span was assessable: none crossed one of umlsmatch's sentence boundaries.

**What it shows:** hybrid adds the attributes mmlite cannot assess, on mmlite's concepts.
Whether umlsmatch's negation beats NegEx on those concepts **cannot be decided here**: the gap
is umlsmatch's higher recall and lower precision, and cTAKES' labels are measurably wrong in
exactly that direction (review-of-systems negatives). Its gate needs adjudicated or human
labels.

## What a gold set needs

Tool output cannot meet the Phase 1 gate; a human-annotated set can. The tooling is here:
`cuiflow gold convert` (a worksheet with one row per mention, plus the notes, to reference
JSONL, reporting every problem by row) and `cuiflow gold validate`. Annotators type the words;
the converter finds the offsets. The annotation guidelines are kept with the gold set, outside
this repository.

Then score with `cuiflow evaluate --format cuiflow --reference gold.jsonl`.

What still has to come from people:
- **Notes:** synthetic, or de-identified and approved. 50–100 notes, with extra notes chosen for
  rare attributes.
- **Two annotators** for calibration and a 20% overlap.
- **An adjudicator.**

## Gold set

**Read the caveat first.** The gold set is Claude-drafted: 30 synthetic notes, 843 mentions.
doc_01–20 are the three annotators' notes adjudicated by Claude; doc_21–30 are notes Claude
wrote, annotated twice and adjudicated. The project owner signed the set off without a row-by-row
review. It is not human-annotated, so every figure below is agreement with this reference, not
accuracy. Reports: [eval/2026-09-24-gold/](eval/2026-09-24-gold/),
[eval/2026-09-24-gold-filtered/](eval/2026-09-24-gold-filtered/).

```powershell
cuiflow evaluate --format cuiflow --reference <gold.jsonl> `
    --modes single:mmlite,single:umlsmatch,ensemble:union,ensemble:consensus,ensemble:hybrid_staged `
    --out docs/eval/<date>-gold --per-document
```

The same indexes as the baseline.

| mode | pred | overlap P / R / F1 | CUI-set F1 |
|---|---:|---|---:|
| `single:mmlite` | 5818 | 0.104 / 0.717 / 0.181 | 0.195 |
| `single:umlsmatch` | 2928 | 0.218 / 0.756 / 0.338 | 0.382 |
| `ensemble:union` | 7077 | 0.096 / **0.805** / 0.172 | 0.191 |
| `ensemble:consensus` | 1670 | **0.337** / 0.667 / **0.447** | 0.459 |
| `ensemble:hybrid_staged` | 5818 | 0.104 / 0.717 / 0.181 | 0.195 |

| attribute (positives) | `single:mmlite` | `single:umlsmatch` | `ensemble:hybrid_staged` |
|---|---|---|---|
| negated (129–132) | 0.989 / 0.705 / 0.824 | 0.975 / 0.871 / **0.920** | 0.974 / 0.868 / 0.918 |
| history_of (51–56) | not assessed | 0.708 / 0.304 / 0.425 | 0.650 / 0.255 / 0.366 |
| subject (42–43) | not assessed | 0.778 / 0.500 / 0.609 | 0.778 / 0.488 / 0.600 |
| uncertain (26–27) | not assessed | 0.864 / 0.704 / 0.776 | 0.857 / 0.692 / 0.766 |
| conditional (59–60) | not assessed | not assessed | not assessed |

(P / R / F1 on overlap-matched pairs. `conditional` is umlsmatch's opt-in prototype and is off in
these runs.)

### Why precision is low for every mode

The guidelines annotate one concept per mention, at the longest span, and leave out chart
furniture, people, qualifiers and laboratory results. The engines do none of that:

| | `single:mmlite` | `single:umlsmatch` |
|---|---:|---:|
| predictions in scope | 5818 | 2928 |
| touching no gold span | 4542 | 1276 |
| extra CUIs on an already-predicted span | 3005 | 565 |
| overlapping a gold span, different boundaries | 358 | 873 |
| same span as a gold mention | 918 | 779 |

The most frequent predictions touching no gold span are "patient", "assessment", "with", "for",
"sex", "normal", "date" and "plan" (mmlite), and "sex", "plan", "history", "assessment" and
"history of" (umlsmatch). Most of mmlite's precision loss is headings, template words and
several candidate CUIs per span. It is a difference of convention, not of recognition, and a
post-filter can remove most of it
([Guideline post-filter](#guideline-post-filter-mention_filter--guidelines)).

### Phase 3 gate (margin fixed before scoring: 0.02 F1)

The better single engine on this set is `single:umlsmatch` (overlap F1 0.338).

| mode | target metric | beats both single engines? | F1 vs 0.338 | gate |
|---|---|---|---|---|
| `ensemble:union` | recall | yes: 0.805 vs 0.756 and 0.717 | 0.172 (−0.166) | **fails** |
| `ensemble:consensus` | precision | yes: 0.337 vs 0.218 and 0.104 | 0.447 (+0.109) | **passes** |
| `ensemble:hybrid_staged` | `negated` F1 | no: 0.918 beats mmlite (0.824), not umlsmatch (0.920) | 0.181 (−0.157) | **fails** |

`ensemble:consensus` still counts as experimental until the pass holds on real notes
([What the gold-set results show](#what-the-gold-set-results-do-and-do-not-show)). Keep in mind
how it wins: it gains its precision mostly by requiring both engines, which removes much of
mmlite's template noise described above, and the reference is Claude-drafted. The default stays
`single:umlsmatch`. Making consensus the default is a separate decision, because it gives up
recall (0.667 vs 0.756).

Hybrid's concept figures are mmlite's by design. Its question is whether umlsmatch's rules
beat NegEx on mmlite's spans, and here they do (negated F1 0.918 vs 0.824). They are 0.002 short
of umlsmatch's own figure, well inside the noise at 130 positives, but the gate asks for a win.

## What the gold-set results do and do not show

**"Passes" means passes on a synthetic, Claude-made reference.** Claude drafted the 30 notes,
annotated them, annotated doc_21–30 a second time, and adjudicated both; the owner signed the
set off without a row-by-row review. The κ of 0.93–1.00 between the two annotations measures one
model's self-consistency. So the consensus result says that consensus agrees better with one
model's reading of 30 notes written in one style. It says nothing yet about real notes, and
consensus stays an *experimental* mode until the Phase 6 real-note check repeats the result.

**Parts of the gold set were not blind to the scores.** The 0.02 margin was fixed before any
gold-set score was seen, and the first annotation of all 30 notes was signed off before it was
scored. Some work came later, by the same author, with those first scores known: the second
annotation and adjudication of doc_21–30, guidelines 1.2 (§9.9–9.12), and one correction to
doc_03 under §9.9. The records guard against steering but cannot rule it out: the second
annotation was made from the notes, the guidelines and UMLS only; every adjudication decision
cites a guideline rule or precedent, and none cites engine output. What bounds the risk is the
size of the change: that later work moves no overlap F1 by more than 0.001 and no recall by more
than 0.006, and the gate outcome is the same before and after it.

**Assertion figures compare only at equal coverage.** A reference label whose predicted mention
is "not assessed" counts toward coverage and, if the reference is positive, as a false negative;
if the reference is negative, it changes coverage only. P / R / F1 are therefore comparable
between modes only where their coverage is the same. For `negated`, the one attribute the gate
compares, every mode's coverage is 1.0. For the others, mmlite is "not assessed" (coverage 0),
and `ensemble:union` covers about 0.94 of the reference labels to the other modes' 1.0; each
report gives coverage next to P / R / F1.

## Guideline post-filter (`mention_filter = "guidelines"`)

The precision analysis above says most false positives are conventions, not recognition errors.
`cuiflow.core.filters` applies the guidelines' rules to engine output: headings (3.4),
out-of-scope concept types and template words (3.3, 4.4), one concept per span with SNOMED CT or
RxNorm-coded CUIs first (3.1, 4.3), and the longest span (3.1). It is opt-in:
`--mention-filter guidelines`.

**How it was kept honest.** The rules come from the guidelines and from engine output on the
unlabelled scale corpus, not from gold-set errors. They were fixed before the filter was scored
on the gold set. doc_21–30 are reported separately, because neither engine was developed on
them. **doc_01–20 are the same notes as umlsmatch's own development set**
(`umlsmatch/free_texts/synthetic`), which favours umlsmatch on the full set.

```powershell
cuiflow evaluate --format cuiflow --reference <gold.jsonl> `
    --modes single:mmlite,single:umlsmatch,ensemble:union,ensemble:consensus,ensemble:hybrid_staged `
    --terminology-db data/terminology.sqlite --mention-filter guidelines --out <dir>
```

| mode | all 30 notes: P / R / F1, no filter → filter | held-out doc_21–30: F1, no filter → filter |
|---|---|---|
| `single:mmlite` | 0.104 / 0.717 / 0.181 → 0.340 / 0.673 / **0.452** | 0.275 → **0.647** |
| `single:umlsmatch` | 0.218 / 0.756 / 0.338 → 0.410 / 0.663 / **0.507** | 0.402 → **0.676** |
| `ensemble:union` | 0.096 / 0.805 / 0.172 → 0.335 / 0.681 / **0.449** | 0.236 → **0.650** |
| `ensemble:consensus` | 0.337 / 0.667 / 0.447 → 0.464 / 0.627 / **0.533** | 0.594 → **0.700** |
| `ensemble:hybrid_staged` | as `single:mmlite` | as `single:mmlite` |

Reports: [eval/2026-09-24-gold-filtered/](eval/2026-09-24-gold-filtered/),
[eval/2026-09-24-heldout-none/](eval/2026-09-24-heldout-none/),
[eval/2026-09-24-heldout-guidelines/](eval/2026-09-24-heldout-guidelines/).

**What it costs.** Recall falls by about 0.04 (mmlite) to 0.09 (umlsmatch). On the full set,
umlsmatch loses 78 gold mentions it found without the filter:

- 44 to the longest-span rule, mostly drug names inside a longer drug string umlsmatch also
  matched ("Atorvastatin" inside "Atorvastatin 20 mg"). The guidelines annotate the drug name
  only (3.5), so the rule should not apply there.
- 34 to one-concept-per-span choosing a different CUI from the gold's ("chemotherapy",
  "numbness", "sore throat"). The gold's tie-break also compares preferred names (4.3 step 2),
  which the filter cannot.

None is lost to a heading.

The drug-name exception is an obvious fix, but it was found by looking at gold errors. It needs
new notes to be measured fairly, so it is not in this filter.

**The Phase 3 gate with the filter** (better single engine: `single:umlsmatch`, F1 0.507):

- `union`: recall 0.681 beats 0.673 and 0.663, but F1 drops 0.058. **Fails.**
- `consensus`: precision 0.464 beats 0.410 and 0.340, and F1 rises 0.026. **Passes.**
- `hybrid_staged`: `negated` F1 0.915 edges umlsmatch's 0.914 and mmlite's 0.833, but concept
  F1 drops 0.055. **Fails.**

Same outcome as without the filter. Assertion figures barely move (umlsmatch `negated` F1 0.920 →
0.914 on fewer matched pairs).

**What the gain means.** The filter encodes the guidelines the reference was annotated with, so
most of its 0.1–0.3 F1 is agreement with those conventions (no headings or template words, one
CUI per span, the longest span), not better concept recognition. It shows how much of each
engine's apparent error is convention. It does not show that the filtered output recognises
more concepts correctly, and a reference built under other conventions would reward it less.

**Recommendation.** Use `--mention-filter guidelines` when output should look like the
guidelines' annotation, for example to compare against human annotation or to load into OMOP
`NOTE_NLP`. Its effect on these scores is larger than the difference between any two modes, for
the reason above. Whether it becomes the default is a separate decision, best taken after the
drug-name exception is measured on new notes.
