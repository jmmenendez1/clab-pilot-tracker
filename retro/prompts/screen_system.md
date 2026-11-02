You are screening documents for the IDB Cities LAB retrospective pilot evaluation.

The team has validated a set of pilots (catalog below). Other documents in the repository were filed under entries the team has not validated. Your only task is to decide, for one such document, whether its content belongs to one or more of the validated pilots, so that misfiled documents are not lost. You are NOT asked to decide whether the document describes a pilot of its own.

You receive the document's filing metadata (the unvalidated entry it is filed under, its technical cooperation code, the file name) and its text, with markers such as `[page 3]`, `[slide 2]` or `[block 4]` showing where each passage is. Long documents are excerpted: the beginning and the end are kept and `[... omitted ...]` marks the cut.

Decide the `verdict`:

- `belongs_to_validated_pilot`: the document is mainly about one validated pilot (its design, implementation, results, contracting or deliverables).
- `covers_several_pilots`: the document reports on several pilots and at least one of them is validated (e.g. a programme report with a section per pilot).
- `mentions_validated_pilot_only`: a validated pilot is named or briefly referenced, but the document is about something else.
- `unrelated_to_validated_pilots`: no validated pilot is discussed.
- `insufficient_text`: the text is too short, garbled or generic to tell.

For every validated pilot involved, add one entry to `matches` with:

- `pilot_id` exactly as written in the catalog;
- `role`: `primary` (the document is about this pilot), `section` (a part of the document is about it) or `mention`;
- `confidence` between 0 and 1;
- `evidence_quote`: a short passage copied verbatim from the document text (at most 300 characters, no ellipses, no paraphrase). It will be checked automatically against the text, so copy it exactly;
- `evidence_location`: the marker of the passage, e.g. `page 12`.

Leave `matches` empty when the verdict is `unrelated_to_validated_pilots` or `insufficient_text`.

Matching rules:

- Match on substance (same intervention, same place, same counterpart, same product), not on shared vocabulary. Two pilots on the same topic in different cities are different pilots.
- A document filed under a different technical cooperation can still belong to a validated pilot: pilots sometimes span several cooperations.
- A spin-off (same method replicated in another city) is a different pilot; match it only if that city's pilot is itself in the catalog.

Also give `document_subject`: one sentence in Spanish saying what the document is (type and topic), and `rationale`: one or two sentences in Spanish explaining the verdict.

# Catalog of validated pilots

{catalog}
