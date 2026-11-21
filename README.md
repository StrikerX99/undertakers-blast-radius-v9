# Blast Radius selector

The entry selects tests for independent patches against SQLGlot at
`eb11aec4be8cb2122afe713a4816662c3f0fb2fa`. Build with the Dockerfile at
this directory's root. At runtime, it reads mounted patches and `tests.json`
from `/data`, and writes the requested JSON lines to stdout.

If a hunk's reported position disagrees with the source, the selector can
relocate the hunk when every old line matches at one location. Ambiguous
context triggers a conservative fallback. The selector applies every change
in a patch before running a verification probe. It maps edits to baseline
coverage, fixture and source-file reads, dictionary value reads, and recorded
operands of comparisons. For a supported comparison change, it evaluates
the new comparison against the recorded operands. Missing information can
broaden the selection. Small candidate groups can run against the patched
checkout. Only completed, passing tests may be removed. On Linux, a probe can fork a Python process that has loaded the standard library. The child then imports SQLGlot and its tests from the patched checkout.

The scheduler tries one through eight workers, scores each partition in
integer milliseconds, and emits the cheapest one it found. Empty selections
use no workers. The Docker build downloads the pinned SQLGlot archive and
precompiles its Python code. It also partitions the plain coverage map by source file, so each run loads only the portions it needs. Hash-based bytecode validation makes Python
recompile changed source in the temporary checkout.

## How the selector uses the baseline

Short-circuit operands, conditional branches and comprehension elements have their own test coverage. Changes can be narrowed to the tests that evaluated an expression. For changes to scalar class settings, the map distinguishes reads of the value from uses in comparisons or boolean conditions. Derived date formatting maps have dependencies across SQLGlot dialects, including inverse lookups. Source-reading tests retain their own dependencies even when a module compiles identically.

On eligible changes to nested expression builders, a child process replays observed primitive inputs and compares the complete supported expression graph. For fixture-only and method-local changes, the selector can compare the records produced by the pinned fixture loaders and run inexpensive candidate methods in the complete patched checkout. The isolated method environment includes the MIT-licensed mypy_extensions package used by the reference suite.

The TPC-DS executor test submits independent SQL queries and compares the Python result with DuckDB. The baseline collector runs the passing test, observes comparison operands per query, and fingerprints each returned table and generated DuckDB statement. For a supported optimizer comparison edit, the selector runs affected queries with the pinned fixture tables through the patched execution code. It requires exact equality of ordered results, column names and the SQL submitted to DuckDB. Unobserved operand values retain their queries. Edits to the corpus, changes outside the supported comparison, excessive replay cost or an incomplete result keep the original test schedule. The map records passing baseline executions; runtime decisions use the supplied patch.

## Regenerating the maps

The committed maps were generated from the unmodified pinned checkout. The
Docker build uses them as plain JSON files. To regenerate them offline,
use a Linux development machine with Python 3.13. Allow time for the full
instrumented suite, including the large parser stress test.

```bash
python3.13 -m venv .venv
.venv/bin/pip install -r requirements-build.txt
.venv/bin/python fetch_baseline.py ../sqlglot-baseline
SQLGLOT_ROOT="$PWD/../sqlglot-baseline" OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 \
  .venv/bin/python build_map.py coverage-map.raw.json
.venv/bin/python tools/source_reads.py ../sqlglot-baseline coverage-map.raw.json "$PWD/source-reads.json"
.venv/bin/python tools/import_lines.py ../sqlglot-baseline coverage-map.raw.json "$PWD/import-lines.json"
.venv/bin/python tools/finalize_map.py coverage-map.raw.json ../sqlglot-baseline source-reads.json import-lines.json coverage-map.json
.venv/bin/python tools/build_config_coverage.py ../sqlglot-baseline coverage-map.json config-reads.json
PYTHONPATH="$PWD" OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 \
  .venv/bin/python tools/build_dispatch_coverage.py ../sqlglot-baseline coverage-map.json dispatch.json
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python tools/profile_external.py ../sqlglot-baseline dispatch.json dispatch.json
```

The following commands regenerate the additional baseline observations. Run them from the submission directory after completing the commands above. Keep the virtual environment first on PATH so tests that spawn Python use the pinned dependencies.

```bash
export PATH="$PWD/.venv/bin:$PATH"
export PYTHONPATH="$PWD"
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 SKIP_INTEGRATION=0
python tools/build_relations.py ../sqlglot-baseline coverage-map.json set-relations.json
python tools/build_gates.py ../sqlglot-baseline coverage-map.json gates.json
python tools/build_fixture_views.py ../sqlglot-baseline coverage-map.json fixture-views.json
python tools/build_scalar_coverage.py ../sqlglot-baseline coverage-map.json scalars.json
python tools/build_time_dependencies.py ../sqlglot-baseline coverage-map.json time-dependencies.raw.json
python tools/profile_time_readers.py ../sqlglot-baseline time-dependencies.raw.json time-dependencies.json
python tools/build_replay.py ../sqlglot-baseline coverage-map.json replay.json
python tools/build_queries.py ../sqlglot-baseline executor-queries.json coverage-map.json
python tools/reference_imports.py reference-imports.json
```

The build generates the bytecode fingerprint index with the same interpreter, compiler flags and source paths used at runtime. The local verification kit contains practice inputs, measured failure lists and scoring tools.

## Version 5

The selector records the actual operand at high-cardinality literal-equality comparisons on the unmodified baseline. It also records conservative per-test operand ranges at high-cardinality integer comparisons. For a supported literal or integer-threshold edit it retains tests whose values could change the result, along with unknown operands and opaque readers. The collector runs the passing pytest suite, including spawned process-pool tasks.

For a single edit to a literal dictionary key in an expression argument lookup, the selector checks whether both keys returned the identical object on every observed baseline invocation. It retains opaque processes and source-text readers.

For a change to an exception handler, the selector compares the old and new catch types against the exception classes observed at that protected block. If a change could stop catching an exception or if a newly caught exception was seen anywhere, the selector retains the existing candidates. Positive evidence of an import-time change can select the entire suite. This also covers cases where the test that first raises an exception affects later tests.

For selected optimizer edits, a separate verification step replays the affected baseline TPC-DS queries through the patched Python optimizer and compares the complete supported expression graph passed to the unchanged planner. Reachability outside this boundary, a changed graph, an unsupported edit, or an incomplete replay retains the executor test. CPU is budgeted against the simulated time that could be saved. The runtime also includes the exact public pytz wheel from the reference environment so that applicable BigQuery unittest methods can be executed in their actual patched form.

For a nonempty schedule, tests with zero recorded duration can be added at zero simulated cost. The scheduler also considers other tests that read the changed files where they fit within the busiest worker's load. Only the supplied test catalog is used to calculate durations.

Run the following from the submission directory after generating the baseline coverage map and executor query map as described above. Adjust the path to the official starter catalog for your local checkout.

```bash
export PATH="$PWD/.venv/bin:$PATH" PYTHONPATH="$PWD"
export SKIP_INTEGRATION=0 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 PYTHON_CPU_COUNT=1
python tools/build_arg_lookups.py ../sqlglot-baseline coverage-map.json arg-lookups.json ../blast-radius-sample/sample/tests.json
python tools/build_exceptions.py ../sqlglot-baseline coverage-map.json exceptions.json
python tools/build_literals.py ../sqlglot-baseline coverage-map.json literals.json
python tools/build_checkpoints.py ../sqlglot-baseline coverage-map.json optimizer-checkpoints.json
```

## Version 6

The parser token profile records which token was current when a parser called the pinned SQLGlot Parser._match with a literal TokenType member. A single changed member can affect the result, token advancement or comment attachment only when the current token matches the old or the new member. The selector retains both executor tests whenever its baseline observations encounter either member or an unknown implementation. Source readers and unobserved sites keep their original schedule.

The collector runs real TPC-DS and TPC-H tests, including setup, teardown, reference queries and spawned workers. It also collects every test module and runs the entire executor test class, then conservatively unions those tokens with the individual case observations. Baseline hashes are checked before tracing. Run from the submission directory with the locked reference packages installed.

```sh
python tools/build_tokens.py ../sqlglot-baseline coverage-map.json token-cases.json
```

For a small group of candidate unittest methods on a single edited production file, the selector can run those methods in the full patched checkout and keep any that fail or skip. It limits this additional path to six candidates and a combined recorded duration of 50 ms, verifies pending imports, and watches for missing reference packages.

When an edited class setting changed an observed comparison during module initialization, the selector keeps the complete supplied catalog. Module globals can carry the changed value into tests that never read the setting directly. For a separate class of single, same-type literal edits in dialect settings, baseline instrumentation records class creation, resolved reads and opaque class-namespace accesses. The selector can narrow the schedule only for observed declarations with no recorded consumers and no opaque or raw-source readers. Positive dependencies from the original map remain selected.

Reproduce the source-access and dialect-setting observations on Linux using the pinned baseline, the reference dependencies listed above and the numbered catalog from the official starter.

```bash
python tools/build_unopened_sources.py ../sqlglot-baseline coverage-map.json ../blast-radius-sample/sample/tests.json unopened-sources.json
python tools/build_dialect_reads.py ../sqlglot-baseline coverage-map.json ../blast-radius-sample/sample/tests.json dialect-reads.json
```

## Version 7

The selector prices a proposed executor verification by scheduling the current candidate set with and without the integration test. A forked child replays the patched computation within a CPU and wall-time budget. It can remove the test only after completing the applicable checks.

For optimizer edits, the baseline records which of the submitted queries executed each observed optimizer function. The child replays the affected queries against the complete patch, compares their optimized expression graphs at the planner boundary, and retains the test on a changed or unsupported graph. Separate observations from real spawned workers identify modules that the executor never loads.

For edits confined to generation of the DuckDB reference query, the child rechecks the 57 executed query cases. It accepts the same SQL text, or a change confined to the outer sort's NULL placement when at most one fixture row enters that sort. The baseline collector measures this cardinality before ORDER BY, OFFSET or LIMIT. It hashes the driver, helpers and fixture data. Changed query results, changed inner SQL, a possible change to the worker computation, or an incomplete check preserve the test.

A full-suite Linux trace records which tracked documentation and fixture files remain unread on the pinned revision. The trace distinguishes a check for whether the path is a directory from a read of its contents. A content-only edit preserves the directory type. Other observed metadata access keeps the file ineligible. This can eliminate a schedule on a patch consisting entirely of verified unused data. A Python compiler check also recognizes unchanged instruction streams whose column offsets differ only on a few non-dispatching instructions. Its guard checks the pinned source-access profile. For a missing top-level dialect import, a fresh interpreter verifies which binding is missing. Only expensive tests with independent cold and warm module traces can be removed by that check.

To reproduce these observations, use a clean Git checkout of the exact pinned commit, the official test catalog, and the native environment described above. Run these commands from the submission directory.

```bash
python tools/build_data_access.py "$PINNED_GIT" "$CATALOG" unread-data.json
python tools/build_import_footprints.py "$PINNED_GIT" "$CATALOG" import-footprints.json
python tools/build_companions.py "$PINNED_GIT" optimizer-checkpoints.json executor-unused-modules.json
python tools/build_phase.py "$PINNED_GIT" . reference-phases.json
python tools/build_patterns.py "$PINNED_GIT" . reference-rows.json
```

The submitted Docker build consumes the committed baseline observations. Local validation patches and failure lists live in the separate audit archive.

The optimizer guard profile records the truth and type of primitive string fields at simple SQLGlot expression-type checks in the pinned suite. A single added short-circuit predicate can leave the original control flow identical if the original type check failed or the new field check always succeeds. The selector retains tests with uncertain fields, different observed outcomes, source reads, or opaque processes. The collector traces native process-pool tasks with their submitting test identity and keeps unattributed events conservative.

With the pinned reference dependencies installed and the coverage map available, regenerate the profile offline from the submission directory.

```bash
python tools/build_guard_inputs.py ../sqlglot-baseline coverage-map.json ../blast-radius-sample/sample/tests.json guard-inputs.json
```

## V8 dependency and optimizer checks

The module dependency map records on-disk inputs of real pytest groups on the pinned checkout. Large tests have their own groups. For ordinary leaf parser classes and eligible generator method bodies, the selector combines these cold inputs with the existing full-suite execution and source-read evidence. Ownership checks and tests that inspect the dialect registry retain dependencies outside the immediate dialect. Test-source changes also consult a reverse test import graph so an import error in a shared test module retains its consumers.

An independent profile tracks which numbered tests accessed the literal schema or documentation of leaf SQLGlot expression classes. It includes reads through instances and class objects. The parser profile records method calls, throws, and observed outcomes of selected guards and optional arguments on the passing baseline. All tests affected by an unrepresented or opaque case retain their previous eligibility.

For supported optimizer rewrites, the selector executes affected public TPC-DS inputs against the actual patch. If the complete optimized expression changes, it compares the real Python executor's ordered rows, column names and reference SQL with the passing baseline. A timeout, error or mismatching result retains the expensive test. The selector estimates the worker seconds it could save before paying for this verification. The local answer keys and validation patches are distributed separately.

Use the existing pinned development environment to regenerate these baseline observations. The script inputs and profile fields are documented in the offline collectors under `tools/`.

For the reverse imports, expression declarations, dialect module footprints and optimizer collection profile, run these commands using the same pinned development environment and official test catalog as the other maps.
```sh
python tools/build_test_imports.py "$PINNED_GIT" test-imports.json
python tools/collect_optimizer_context.py "$PINNED_GIT" "$CATALOG" optimizer-collection.json
python tools/build_expression_schemas.py "$PINNED_GIT" coverage-map.json "$CATALOG" expression-schemas.json
python tools/build_module_footprints.py "$PINNED_GIT" "$CATALOG" module-footprints.json "$WORK/footprints" --jobs 2
```

The parser observer runs the clean, pinned SQLGlot test suite. It records which test calls each parser method, which simple conditional blocks execute, and where a token matcher consumes an optional expression. The observer follows submitted multiprocessing tasks. It attributes collection-time and unattributed activity conservatively. Each map entry describes an observation on the pinned source.

For a qualifying parser edit, the selector verifies that the bytes outside the affected methods are identical, compiles the changed methods, and checks their local and closure scopes. It can narrow a change to the tests that used those methods or observed the altered behavior. Raw-source readers, opaque test processes, and ambiguous observations keep the wider selection.

To reproduce the map on Linux with the full native requirements installed, run these commands from the submission root. Supply the absolute path to the pinned Git checkout and the official catalog.

```bash
python tools/build_parser_effects.py "$PINNED_GIT" "$PWD" "$CATALOG" /tmp/parser-observations.json
python tools/finish_parser_evidence.py /tmp/parser-observations.json "$CATALOG" parser-evidence.json
```

The final selection step restores consumers of changes to ordered expression schemas and named generator transforms. A positional SQL function constructor consumes schema keys in order. The generator profile records the concrete generator class and expression class encountered at dispatch, including calls during test collection. Regenerate that profile from a clean checkout with the native test dependencies installed.

```bash
python tools/build_generator_dispatch.py "$PINNED_GIT" "$CATALOG" generator-dispatch-reads.json
```

## Inherited dialect methods and changes spanning a dialect

When an ordinary method override is removed from a leaf dialect, the selector uses calls to the original method, concrete generator dispatch and potential users that inspect its class or source. An offline scan checks the pinned method and its inherited implementation. For eligible changes spanning the parser, generator and type declarations for one dialect, the selector combines all three files' test dependencies. Each test group that opened one of the files in a fresh process is included, along with additional registry and source readers. Unsupported changes retain the existing selection.

```sh
python tools/build_inherited_methods.py "$PINNED_GIT" module-footprints.json inherited-methods.json
python tools/build_dialect_value_names.py "$PINNED_GIT" dialect-value-names.json
```

For larger combinations of ordinary edits inside a single dialect parser or generator, the selector validates the class declarations and their import-time expressions. A dependency recorded in any cold pytest group or in the full-suite reader profile retains the relevant tests. If the candidate change saves enough scheduled time, two isolated processes load the clean and patched dialect. They compare shared parser dispatch, generator transforms and type mappings, tokenizer keywords, registered dialects, expression metadata and expression schemas. An unsupported value, import error, timeout or changed fingerprint keeps the earlier schedule.

```sh
python tools/build_initializer_bindings.py "$PINNED_GIT" initializer-bindings.json
python tools/make_class_manifest.py "$PINNED_GIT" module-footprints.json ordinary-dialect-classes.json
```

For a dispatch redirection that preserves every key, the selector can also consult observed calls to the old handler and its ordinary private methods. Tests that inspected the class, dispatch registry or source code remain eligible.

## V9 selection and verification

The selectors use observations made on the clean, pinned checkout. An incoming patch is parsed and checked against its original source before these observers can narrow the plan.

A dialect generator can delegate to private module helpers or inherit a method from its base class. The generator symbol index and native call observations account for callers, subclasses, reflected names and dispatch entries when one or more related generator files change. New definitions and unused imports are checked against the unchanged class structure. Candidate sets are preserved when the patch falls outside the supported scope.

The optimizer verifier records intermediate expression graphs on the passing public executor queries. For a supported rule it can stop replay after the last changed rule when the complete graph matches its baseline. A failed check falls back to the prior verifier. The import verifier skips a subprocess when the full suite would be selected on either import outcome.

For parser calls that match a sequence of token texts, the lookahead profile records which tokens the native test encountered after consuming the preceding arguments. An eligible single-literal edit retains a test when its old or new comparison can take a different path. The observer records failed comparisons, collection activity and child processes. Uncertain observations preserve the wider selection. The million-node parser test is retained whenever it was already a candidate.

The dialect setting observer distinguishes a direct read from a read controlling two equivalent conversions of an ordinary ASCII identifier. Only a pinned string table and observed ASCII input qualify. The observer distinguishes class-name enumeration and a read of a known, unrelated namespace key. Other value reads, unknown owners, source consumers and reflective tests keep their dependencies. The unobserved stress method stays eligible.

Regenerate the profiles in the pinned native development environment. The SQLGlot checkout, official catalog and submission directory should all be absolute paths.

```sh
python tools/build_generator_symbols.py "$PINNED_GIT" generator-symbols.json
python tools/build_generator_hierarchy.py "$PINNED_GIT" generator-hierarchy.json
python tools/build_generator_contexts.py "$PINNED_GIT" "$CATALOG" generator-contexts.json
python tools/build_lookahead.py "$PINNED_GIT" "$PWD" "$CATALOG" parser-lookahead.json --retain-memory-test
python tools/build_flag_inputs.py "$PINNED_GIT" coverage-map.json "$CATALOG" flag-inputs.json --retain-memory-test
python tools/build_optimizer_suffix.py "$PINNED_GIT" "$PWD" optimizer-suffix.json
```

## V9.1 selection

For a changed leaf parser class, the selector can follow removal of an inherited token set through aliases and read-only consumers. It requires an eligible declaration, an unchanged class layout, source and registry readers, and clean and patched import checks. Rebuild the static table with `python tools/build_parser_sets.py "$PINNED_GIT" parser-sets.json`.

For a change to an expression return-type declaration, the selector considers all tests observed to read the changed expression metadata. Iteration and other unclassified reads retain their test identities, and the memory-stress method is retained if otherwise eligible. Rebuild with `python tools/build_metadata_inputs.py "$PINNED_GIT" coverage-map.json "$CATALOG" metadata-inputs.json`.

An edit confined to an unentered branch body under an unchanged condition in a Simplifier method can leave the public executor queries unaffected. The verifier checks function and code structure and consults native branch observations across the parent and spawned workers. Rebuild with `python tools/build_simplifier_suites.py "$PINNED_GIT" "$PWD" simplifier-suites.json`.

Python determines whether a callable is a generator or coroutine at compile time. A yield inside an unentered branch can change an ordinary function into a generator for every caller. The final selection checks the compiled callable kinds on relevant edits and widens the schedule when they change.
