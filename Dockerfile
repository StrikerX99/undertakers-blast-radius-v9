FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
WORKDIR /app
COPY requirements-runtime.txt /app/requirements-runtime.txt
RUN python -m pip install --no-cache-dir --disable-pip-version-check --index-url https://pypi.org/simple --no-deps --only-binary=:all: --require-hashes -r /app/requirements-runtime.txt
COPY fetch_baseline.py /app/fetch_baseline.py
RUN python /app/fetch_baseline.py /opt/sqlglot
RUN python -m compileall -q --invalidation-mode checked-hash /opt/sqlglot/sqlglot /opt/sqlglot/tests
COPY method-deps/mypy_extensions.py method-deps/LICENSE /app/method-deps/
COPY reference_probe.py reference-rows.json reference-phases.json /app/
COPY test_dependencies.py test-imports.json /app/
COPY expression_schema.py expression-schemas.json module_footprints.py module-footprints.json optimizer_replay.py optimizer-collection.json parser_evidence.py parser_sites.py parser-evidence.json structural_dependencies.py generator-dispatch-reads.json /app/
COPY inherited_methods.py inherited-methods.json composite_dialects.py dialect-value-names.json /app/
COPY general_dialects.py dialect_initializers.py initializer-bindings.json ordinary-dialect-classes.json shared_tables.py /app/
COPY optimizer_suffix.py optimizer-suffix.json flag-inputs.json flag_inputs.py parser-lookahead.json parser_lookahead.py dispatch_helpers.py generator-symbols.json generator_contexts.py generator-contexts.json generator-hierarchy.json /app/
COPY parser_sets.py parser-sets.json metadata_inputs.py metadata-inputs.json simplifier_suites.py simplifier-suites.json callable_kind.py /app/
COPY guard_inputs.py guard-inputs.json /app/
COPY input_evidence.py unread-data.json import-footprints.json /app/
COPY exception_impact.py exceptions.json /app/
COPY arg_lookups.py arg-lookups.json /app/
COPY dormant_settings.py dialect-reads.json unopened-sources.json /app/
COPY token_impact.py token-cases.json /app/
COPY literal_impact.py literals.json /app/
COPY executor-unused-modules.json optimizer_checkpoints.py checkpoint_graph.py optimizer-checkpoints.json /app/
COPY executor_fixtures.py /app/
COPY set-relations.json set_relations.py time_dependencies.py time-dependencies.json code_index.py query_impact.py query_case.py executor-queries.json reference-imports.json method_probe.py scalar_coverage.py scalars.json builder_replay.py fingerprint.py forms.py replay.json gated_reads.py fixture_views.py gates.json fixture-views.json logic_guard.py workers.py import_scope.py dispatch.json dispatch_coverage.py source_fast.py insertions.py selector.py prepare_map.py value_profile.py runtime_v2.py forkprobe.py coverage-map.json config-reads.json /app/
RUN python /app/prepare_map.py /app/coverage-map.json /app/map-index && python /app/code_index.py /opt/sqlglot /app/baseline-code.bin
CMD ["python", "/app/selector.py"]
