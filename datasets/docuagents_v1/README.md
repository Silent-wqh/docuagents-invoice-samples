# DocuAgentS invoice sample set

## Purpose

This directory holds the frozen DocuAgentS invoice sample set (version 1.0).
It is a benchmark input for invoice field extraction. It is not a production
data set and it is not a sample of real invoices.

The set gives one answer to three questions:

1. Can a reader return the printed values of a synthetic invoice?
2. Can a reader report a value that the document does not print, without
   inventing a value?
3. Can a reader refuse an out-of-scope document, or route it to a person?

The documents, the gold records, the split table and the evidence map are
frozen together. A change to any of them is a new dataset version, not an
edit.

## Contents

```text
datasets/docuagents_v1/
  documents/                30 original documents (21 PDF, 9 images)
  gold/                     30 gold records, one per document
  text/                     35 page text files, one per page
  previews/pages/           35 page previews, one per page
  previews/contact_sheet.png  one index image of every page
  bases/                    base records for the raster documents
  splits.json               the dev and test split
  matrix.snapshot.json      the frozen matrix facts
  manifest.json             the file manifest and the digests
  reports/check_summary.json the generation run report
  reports/annotation_review.json the frozen annotation review
  annotations.lock.json     the frozen annotation decisions
  README.md                 this file
  boundary_fixtures/        22 file-level fixtures plus their gold and previews
  fault_injection/          50 envelopes and 50 candidate answers
```

The main set has 30 documents on 35 pages: 21 PDF documents and 9 image
documents.

## Main matrix

The subclass is fixed by the sample plan. Every document belongs to exactly
one subclass.

| Subclass | Documents | dev | test | What the subclass tests |
| --- | ---: | ---: | ---: | --- |
| A1 | 6 | 2 | 4 | A clean single-page text invoice |
| A2 | 1 | 0 | 1 | A clean text invoice with a second layout |
| A3 | 2 | 1 | 1 | A two-page invoice: line items on page 1, totals on page 2 |
| A4 | 1 | 0 | 1 | A clean invoice with a printed zero tax (`$0.00`) |
| B1 | 3 | 1 | 2 | A photographed document with no text layer |
| B2 | 3 | 1 | 2 | A scanned document with no text layer |
| B3 | 2 | 1 | 1 | A severely degraded image; the readability gate is not verified |
| C1 | 2 | 1 | 1 | A printed line amount that does not match quantity × unit price |
| C2 | 1 | 0 | 1 | A printed subtotal that does not match the line sum |
| C3 | 1 | 0 | 1 | A printed total that does not match subtotal + tax |
| D1 | 1 | 0 | 1 | A missing tax amount: the value is null and the evidence is not_found |
| D2 | 1 | 1 | 0 | A printed purchase order number and no invoice number |
| E | 6 | 2 | 4 | An out-of-scope document: discount, surcharge, non-USD, many rows, credit note, or multiple invoices |
| **Total** | **30** | **10** | **20** | |

Class C documents keep the printed value in gold. The gold value is the value
on the page, not the arithmetic result. The arithmetic conflict is recorded
in `expected_issues` and `arithmetic`. A reader must not repair the print.

Class D documents record a deliberate absence. The gold value is `null`, the
evidence status is `not_found`, and `searched_pages` names the pages that
were searched. In D2 the printed purchase order number is not the invoice
number.

Class E documents have scope status `UNSUPPORTED`. The expected route is a
person, not an automatic acceptance.

## Boundary and fault fixtures

The boundary fixtures are separate from the 30 main documents. They have
their own manifest, their own gold records and their own previews under
`boundary_fixtures/`.

* 18 content fixtures. Each one isolates one boundary condition, for example
  a missing field, an ambiguous layout or a rounding edge.
* 4 technical fixtures. Each one is a file that a reader must reject: an
  empty file, an encrypted file, a file with a false extension, and the like.
* 4 object-level numeric cases. They live in `tests/fixtures/numeric/` and
  they are covered by pytest only. They are not file-level fixtures and they
  are not in the fixture manifest.

The fault injection suite is separate again. It has 50 cases, 50 envelopes
and 50 candidate answers (100 artifacts). The cases are 5 fault types × 10
cases: `unit_decimal_shift`, `line_digit_transpose`, `missing_row`,
`duplicate_row` and `tax_null`. The split is 15 dev cases and 35 test cases.
The `original` value of an envelope is an evaluation input. It is not a model
input.

## Gold, evidence and codes

Each gold record has `schema_version` 2 and `contract_version` 1.1. The codes
are frozen in `contracts/codes.v1.1.json`. The manifest has `schema_version`
1, `contract_version` 1.1 and `digest_method_version` 1.

The gold record holds:

* `file`: the file name, SHA-256, media type and page count;
* `printing`: the currency header, the date form, the tax note and the
  currency symbol;
* `invoice`: the invoice-level values and the line items, or `null` for a
  multi-invoice file;
* `scope`: the status, the reasons and the signals;
* `readability`: the gate and the human verification state;
* `expected_issues` and `acceptance_policy`: the deliberate decisions;
* `arithmetic`: the printed and recomputed values, with their deltas;
* `evidence`: one entry for every observed, missing, unreadable or ambiguous
  value, with the page and the searched pages.

Evidence status values are `observed`, `not_found`, `unreadable` and
`ambiguous`. A PDF evidence entry carries an approximate box
(`bbox_precision: "approximate"`). An image evidence entry carries no box
(`bbox_precision: "unavailable"`), because the image has no text layer. The
box of a line-item value may be absent. A box is a locator, never proof: the
text comparison is the check.

Every evidence entry has `verification: "generator_recorded"`. It records how
the value was produced. It is not a human confirmation.

## Field scoring

The scoring range is computed from the frozen gold records:

* one decision per invoice-level field: `invoice_number`, `invoice_date`,
  `due_date`, `vendor_name`, `customer_name`, `currency`, `document_type`,
  `subtotal_amount`, `tax_amount`, `total_amount`, `purchase_order_number`;
* one decision per line-item field: `description`, `quantity`, `unit_price`,
  `amount`;
* one decision per charge field: `label`, `amount`, `kind`.

`fields_scored` is **1003 decisions on 28 documents**. Of these, 975 are
non-null values and 28 are explicit null decisions: the reader must return
the same absence, not a guessed value.

The denominator rule is one decision per listed field path. A wrong value, a
missing value when gold holds a value, or an invented value when gold holds
`null` is one wrong decision. There is no partial credit inside a field.

`unscored_field_paths`:

* B3-01 has 23 field paths and B3-02 has 19 field paths. B3 is excluded from
  the score, because the readability gate is not verified (42 paths).
* The 11 invoice-level paths of the multi-invoice file (E-06) are excluded,
  because that file has no single invoice object.
* `printing`, `scope`, `readability`, `acceptance_policy`, `arithmetic`,
  `evidence`, `annotation` and `file` are dataset metadata. They are not
  scored fields.

The 50 injection cases are scored by their own protocol against the candidate
answers. They are not part of the 1003 field decisions.

## Readability

The readability gate is a gold field. The values are:

* `clean_text`: the original has a text layer and the printed values are
  readable;
* `image_readable`: the original is an image and a person can read it;
* `image_degraded_unverified`: the image is degraded and no person has
  confirmed it.

B3 uses `image_degraded_unverified`. B1, B2 and the scanned C1 document use
`image_readable`. The remaining main documents use `clean_text`.

Every main gold record has `readability.verified: false`. The final reading is
a human decision, not a generator decision. The boundary fixture legibility
review (`boundary_fixtures/reports/legibility_review.json`) also stays
`pending_human_confirmation`.

`da-fx-unread` is the unreadable fixture. A white rectangle covers its totals
block. Its `subtotal_amount`, `tax_amount` and `total_amount` are `null`, the
evidence status is `unreadable`, and `must_not_auto_accept` is `true`.

## Split isolation

The split is 10 dev documents and 20 test documents. The split rule keeps the
same source out of both splits:

* `group_id` is unique to one source document;
* `vendor_id` is unique to one vendor identity;
* `base_id` links an image original to its base record;
* the derivation family is the base or the document itself.

No group, vendor, base or derivation family appears in both splits. Vendor
and source identifiers are disjoint. `source_family` is a category (for
example `clean_pdf` or `degraded_photo`), not an identity, so one category can
appear in both splits. `splits.json` and the isolation check record the rule.

## Commands

Run these commands from the repository root. The suite is offline: no
command uses a network, an LLM or a new dependency.

```bash
python -m pytest -q
python -m docuagents_gen.generate --seed 2026 --out <empty-dir>
python -m docuagents_gen.generate --seed 2026 --out <empty-dir> --check \
  --reference datasets/docuagents_v1/manifest.json
python -m docuagents_gen.review --precheck --tree datasets/docuagents_v1
python -m docuagents_gen.review --verify --tree datasets/docuagents_v1
```

`--check` generates a fresh copy in a temporary directory and compares it
with the frozen reference. It never writes the reference. The run prints the
main, fixture and injection counts, then the frozen count lines:

```text
files compared: 142/142
fixture files compared: 64/64
injection files compared: 100/100
frozen files verified: 3/3
lock files verified: 1/1
review files verified: 1/1
readme files verified: 1/1
```

`--freeze` is a maintainer command. It writes the annotation lock and the
frozen review, then updates the manifest. It refuses to run when the machine
pre-check finds a difference.

## Seeds, upstream and environment

* Main set seed: 2026.
* Boundary fixture seed: 2027.
* Fault injection seed: 2028.
* Upstream commit: `3f8864d0d96103a279b5320000c69a71068edd14`
  (Invoice Extraction Benchmark).

The digests depend on these pinned versions:

| Component | Version |
| --- | --- |
| Python | 3.11.17 |
| reportlab | 5.0.1 |
| Pillow | 12.3.0 |
| pypdfium2 | 5.4.0 |
| pdfplumber | 0.11.8 |

`environment.lock.json` records the full environment. PDF text uses the
standard Helvetica, Times and Courier families. The main matrix uses no
handwriting font. The upstream handwriting font (Windows Ink Free,
`Inkfree.ttf`) is not redistributed in this repository.

## Freeze process

1. Generate the set from the pinned seed and the frozen code.
2. Run the machine pre-check on every page. A substantive difference stops
   the process; the check never repairs a source or a gold record.
3. Build `annotations.lock.json`. It binds every source file and every gold
   record by SHA-256 and records the missing, unreadable, ambiguous and
   routing decisions.
4. Build `reports/annotation_review.json`. Its machine part comes from the
   pre-check. Its human fields stay empty.
5. Add the two files and this README to the manifest with the
   `frozen_review` role. The 142 main, 64 fixture and 100 injection artifacts
   do not change.
6. Run the end-to-end check and the test suite.
7. The Project Owner reviews every page preview, signs the review, and only
   then is the release tag created.

The lock and the review are frozen artifacts. A re-run verifies their
recorded hash and their meaning; it does not pretend to review the data
again. A change to a source file invalidates the binding and the check fails.

## Model input restrictions

A runtime extraction process receives only the original files in
`documents/`. It must not receive any of these:

* `gold/` records or any gold value;
* `text/` files or any extracted text;
* `bases/` records;
* `matrix.snapshot.json` or any matrix label;
* `splits.json` or any split label;
* `annotations.lock.json` or its decisions;
* `reports/annotation_review.json` or its expected routing;
* `fault_injection/` envelopes or their `original` values.

The evaluation process reads the gold records and the expected routing. It
runs after the extraction process and never shares state with it. The
injection `original` value and the candidate answer are evaluation inputs,
not model inputs.

## Limitations

* Every document is synthetic. The set does not measure performance on real
  invoices.
* All documents come from one layout engine. Template coverage is limited.
* The degradations are simulated. They are not samples of real camera or
  scanner behaviour.
* The set is not validated against real scans or a business distribution.
* The sample is small: 30 main documents, 35 pages, and few tests per
  subclass. A per-subclass result has a wide uncertainty.
* The fixture set adds 22 file-level cases and 4 object-level cases. It does
  not remove the small-sample limit.
* The public gold may have been seen by people or models. A score on this
  public set is not a blind score.
* A performance conclusion is valid only for this frozen version and the
  registered protocol. It does not transfer to another version or protocol.
* This is a public test set. A reader must state the test visibility when it
  reports a score: the gold and the split are public, so the test split is
  not hidden.

## Licence and attribution

The licences are separate. They are not merged.

* Upstream code in `generator/` and `scorer/`: MIT, copyright (c) 2026
  InvoiceParser Pro. The full text is in `LICENSE`.
* Upstream documents and answer keys in `corpus/` and `examples/`: CC BY 4.0.
  The full text is in `DATA_LICENSE`. Credit: "InvoiceParser Pro, Invoice
  Extraction Benchmark v1".
* New DocuAgentS code, including `docuagents_gen/`: MIT, copyright (c) 2026
  DocuAgentS contributors.
* New DocuAgentS data, including this sample set, its gold records, its
  annotation lock, its review and this README: CC BY 4.0.

Only upstream code was reused to generate the new samples. No upstream
document was copied into this set. `NOTICE` holds the full attribution and
the dependency licences. The upstream licence files are not modified.

## Test visibility

This is a public test set. The documents, the gold records, the split table
and the expected routing are in this repository. A person or a model can read
them before an evaluation. Any reported score must state this visibility. A
blind score needs a private holdout, and this set is not one.

## Human confirmation status

The annotation lock and the frozen review are `pending_human_confirmation`.
`reviewed_by` and `reviewed_at` are empty. No agent signed them.

The Project Owner must review every page preview and the manual checklist in
`reports/annotation_review.json`, then sign both files. The machine pre-check
does not replace that review.

## Release status

The planned tag is `docuagents-v1.0`. The tag is not created by the data gate.
The release record is a draft until the Project Owner signs the review. The
draft is in `reports/release_record_draft.json` at the repository root.
