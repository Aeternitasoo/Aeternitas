# Core file inventory

A Core; B Shared; C Legacy; D Generated; E Sensitive/local; F Uncertain.
Initial files were backed up. Evidence includes imports (including dotted imports), entrypoints, tests and fixture/operations use.
F files remain available pending dedicated review; they were not deleted based on names or age.
Old documentation does not override current AGENTS/README. All original entries pin their initial SHA-256 in the JSON inventory.


| Path | Class | Action | Evidence |
|---|---|---|---|
| .dockerignore | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| .env.example | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| .github/workflows/offline.yml | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| .impeccable/design.json | D | remove_from_current_tree | Generated example/UI review or already-applied debug patch; not runtime input. |
| .impeccable/review/desktop-default.png | D | remove_from_current_tree | Generated example/UI review or already-applied debug patch; not runtime input. |
| .impeccable/review/grove-finish-review.md | D | remove_from_current_tree | Generated example/UI review or already-applied debug patch; not runtime input. |
| .impeccable/surfaces/report-frontend-renderer-py.md | D | remove_from_current_tree | Generated example/UI review or already-applied debug patch; not runtime input. |
| AGENTS.md | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| AI_Research_System_Maturity_Checklist.md | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| AI_Research_System_Progress.md | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| AI_Research_System_v1_Module_Plan.md | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| DESIGN.md | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| Dockerfile | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| NOTICE.md | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| PRODUCT.md | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| RELEASE_CHECKLIST.md | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| RELEASE_NOTES.md | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| aeternitas.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| agent_harness.py | A | retain | Six-core runtime or on-demand Skill. |
| agent_skills.py | A | retain | Six-core runtime or on-demand Skill. |
| agent_skills/evidence-verification/SKILL.md | A | retain | Six-core runtime or on-demand Skill. |
| agent_skills/financial-analysis/SKILL.md | A | retain | Six-core runtime or on-demand Skill. |
| agent_skills/industry-research/SKILL.md | A | retain | Six-core runtime or on-demand Skill. |
| agent_tools.py | A | retain | Six-core runtime or on-demand Skill. |
| analysis_contracts.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| analysis_workflow.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| api_client.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| candidate_registry.py | A | retain | Six-core runtime or on-demand Skill. |
| chatgpt_auth.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| chatgpt_stream.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| company_identity.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| compose.yaml | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| config/companies.json | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| config/container-empty-auth/.keep | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| config/container.env.example | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| config/postgres-init.sh | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| config/research_cloud_compute_power.json | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| docs/ADAPTIVE_ARCHITECTURE_AUDIT.md | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| docs/ADAPTIVE_DATA_COVERAGE.md | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| docs/ADAPTIVE_REVIEW_CONSOLIDATED_20261027.md | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| docs/ADAPTIVE_ROADMAP.md | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| docs/ADAPTIVE_TARGET_ARCHITECTURE.md | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| docs/ARCHITECTURE.md | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| docs/COMPANY_DISCOVERY.md | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| docs/LAB_AGENT_HARNESS_V1.md | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| docs/LAB_AGENT_INFRASTRUCTURE_V1.md | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| docs/LAB_EVALUATION_V1.md | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| docs/LAB_MARKET_DATA_V1.md | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| docs/LAB_PRICE_EXPERIMENT_V1.md | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| docs/LAB_PROSPECTIVE_SNAPSHOT_V1.md | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| docs/LAB_RESEARCH_AGENT_V0.md | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| docs/LAB_RESEARCH_INTELLIGENCE_V1_3.md | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| docs/LAB_RESEARCH_RSI_V1_1.md | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| docs/LAB_RESEARCH_WORKFLOW_V1.md | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| docs/LAB_RSI_INDEPENDENT_VALIDATION_V1_2.md | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| docs/LAB_RSI_V1_3_INTEGRATION.md | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| docs/LAB_TIME_MACHINE_V1.md | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| docs/LAB_VST_CSV_IMPORT.md | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| docs/M1_FINANCIAL_API.md | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| docs/M1_RESEARCH_PIPELINE.md | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| docs/M2_DATA_CONTRACTS.md | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| docs/M3_APPLICATION.md | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| docs/M4_ANALYSIS_CHECKS.md | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| docs/M5_RECOVERY.md | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| docs/M6_EVALUATION.md | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| docs/M7_CHANGE_EXERCISE.md | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| docs/M7_RELEASE.md | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| docs/PACKAGE_README.md | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| docs/REFERENCES_AND_LICENSES.md | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| docs/design/company-discovery.md | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| evals/m6/development/D01.json | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| evals/m6/development/D02.json | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| evals/m6/development/D03.json | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| evals/m6/development/D04.json | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| evals/m6/development/D05.json | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| evals/m6/development/D06.json | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| evals/m6/development/D07.json | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| evals/m6/development/D08.json | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| evals/m6/development/manifest.json | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| evals/m6/holdout-manifest.json | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| evals/m6/holdout/H01.json | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| evals/m6/holdout/H02.json | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| evals/m6/holdout/H03.json | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| evals/m6/holdout/H04.json | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| evals/m6/holdout/manifest.json | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| evals/m6/protocol.json | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| evaluation_cases.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| evidence_repository.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| evidence_selection.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| examples/AAPL/report.html | D | remove_from_current_tree | Generated example/UI review or already-applied debug patch; not runtime input. |
| examples/AAPL/report.json | D | remove_from_current_tree | Generated example/UI review or already-applied debug patch; not runtime input. |
| examples/MSFT/report.html | D | remove_from_current_tree | Generated example/UI review or already-applied debug patch; not runtime input. |
| examples/MSFT/report.json | D | remove_from_current_tree | Generated example/UI review or already-applied debug patch; not runtime input. |
| examples/README.md | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| examples/run-manifest.json | D | remove_from_current_tree | Generated example/UI review or already-applied debug patch; not runtime input. |
| fetch_financials.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| filing_fact_audit.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| filing_source.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| financial_facts.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| harness_contracts.py | A | retain | Six-core runtime or on-demand Skill. |
| infrastructure_rsi.py | A | retain | Six-core runtime or on-demand Skill. |
| lab_agent_batch.py | C | remove_from_current_tree | Retired price-prediction/teaching/one-off experiment and its caller closure; no remaining runtime import. |
| lab_evaluation.py | A | retain | Six-core runtime or on-demand Skill. |
| lab_evaluation_demo.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| lab_price_experiment.py | C | remove_from_current_tree | Retired price-prediction/teaching/one-off experiment and its caller closure; no remaining runtime import. |
| langfuse_adapter.py | A | retain | Six-core runtime or on-demand Skill. |
| local_api.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| m1_analysis_repair_fix.patch | D | remove_from_current_tree | Generated example/UI review or already-applied debug patch; not runtime input. |
| m1_chatgpt_login.patch | D | remove_from_current_tree | Generated example/UI review or already-applied debug patch; not runtime input. |
| m1_financial_api.patch | D | remove_from_current_tree | Generated example/UI review or already-applied debug patch; not runtime input. |
| m1_research_pipeline.patch | D | remove_from_current_tree | Generated example/UI review or already-applied debug patch; not runtime input. |
| m1_stream_header_fix.patch | D | remove_from_current_tree | Generated example/UI review or already-applied debug patch; not runtime input. |
| m4_experiments.py | C | remove_from_current_tree | Retired price-prediction/teaching/one-off experiment and its caller closure; no remaining runtime import. |
| m6_comparison.py | C | remove_from_current_tree | Retired price-prediction/teaching/one-off experiment and its caller closure; no remaining runtime import. |
| m6_frozen_trial.py | C | remove_from_current_tree | Retired price-prediction/teaching/one-off experiment and its caller closure; no remaining runtime import. |
| m7_maintenance.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| market_data.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| migrations/001_evidence.sql | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| migrations/002_company_boundaries.sql | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| migrations/003_research_runs.sql | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| migrations/004_task_boundaries.sql | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| migrations/005_run_recovery.sql | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| migrations/006_frozen_live_evidence.sql | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| news_source.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| point_in_time.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| prompts/m4/common-v1.md | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| prompts/m4/events-v1.md | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| prompts/m4/fundamentals-v1.md | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| prompts/m4/risk-v1.md | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| prompts/m4/synthesis-v1.md | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| prompts/m6/common-v1.md | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| prompts/m6/events-v1.md | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| prompts/m6/fundamentals-v1.md | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| prompts/m6/risk-v1.md | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| prompts/m6/synthesis-v1.md | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| prospective_snapshot.py | C | remove_from_current_tree | Retired price-prediction/teaching/one-off experiment and its caller closure; no remaining runtime import. |
| pytest.ini | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| recoverable_pipeline.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| report_frontend/README.md | C | remove_from_current_tree | Independent report frontend has no imports from retained Research Engine; standard HTML renderer retained. |
| report_frontend/__init__.py | C | remove_from_current_tree | Independent report frontend has no imports from retained Research Engine; standard HTML renderer retained. |
| report_frontend/assets/fonts/InstrumentSerif-OFL.txt | C | remove_from_current_tree | Independent report frontend has no imports from retained Research Engine; standard HTML renderer retained. |
| report_frontend/assets/fonts/NotoSerifSC-OFL.txt | C | remove_from_current_tree | Independent report frontend has no imports from retained Research Engine; standard HTML renderer retained. |
| report_frontend/assets/fonts/download-provenance.txt | C | remove_from_current_tree | Independent report frontend has no imports from retained Research Engine; standard HTML renderer retained. |
| report_frontend/assets/fonts/instrument-serif-italic.ttf | C | remove_from_current_tree | Independent report frontend has no imports from retained Research Engine; standard HTML renderer retained. |
| report_frontend/assets/fonts/instrument-serif-regular.ttf | C | remove_from_current_tree | Independent report frontend has no imports from retained Research Engine; standard HTML renderer retained. |
| report_frontend/assets/fonts/noto-serif-sc-heading.ttf | C | remove_from_current_tree | Independent report frontend has no imports from retained Research Engine; standard HTML renderer retained. |
| report_frontend/assets/logo.svg | C | remove_from_current_tree | Independent report frontend has no imports from retained Research Engine; standard HTML renderer retained. |
| report_frontend/assets/site.css | C | remove_from_current_tree | Independent report frontend has no imports from retained Research Engine; standard HTML renderer retained. |
| report_frontend/assets/site.js | C | remove_from_current_tree | Independent report frontend has no imports from retained Research Engine; standard HTML renderer retained. |
| report_frontend/build.py | C | remove_from_current_tree | Independent report frontend has no imports from retained Research Engine; standard HTML renderer retained. |
| report_frontend/editorial.py | C | remove_from_current_tree | Independent report frontend has no imports from retained Research Engine; standard HTML renderer retained. |
| report_frontend/renderer.py | C | remove_from_current_tree | Independent report frontend has no imports from retained Research Engine; standard HTML renderer retained. |
| report_frontend/serve.py | C | remove_from_current_tree | Independent report frontend has no imports from retained Research Engine; standard HTML renderer retained. |
| report_frontend/test_editorial.py | C | remove_from_current_tree | Independent report frontend has no imports from retained Research Engine; standard HTML renderer retained. |
| report_frontend/test_reader.py | C | remove_from_current_tree | Independent report frontend has no imports from retained Research Engine; standard HTML renderer retained. |
| requirements-memory.lock | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| research_agent.py | C | remove_from_current_tree | Retired price-prediction/teaching/one-off experiment and its caller closure; no remaining runtime import. |
| research_analysis.py | A | retain | Six-core runtime or on-demand Skill. |
| research_contract_profile.py | A | retain | Six-core runtime or on-demand Skill. |
| research_inputs.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| research_intelligence.py | A | retain | Six-core runtime or on-demand Skill. |
| research_memory.py | A | retain | Six-core runtime or on-demand Skill. |
| research_observability.py | A | retain | Six-core runtime or on-demand Skill. |
| research_pipeline.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| research_quality.py | A | retain | Six-core runtime or on-demand Skill. |
| research_report.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| research_rsi.py | A | retain | Six-core runtime or on-demand Skill. |
| research_runtime.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| research_service.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| research_test_support.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| research_validation.py | A | retain | Six-core runtime or on-demand Skill. |
| research_worker.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| research_workflow.py | A | retain | Six-core runtime or on-demand Skill. |
| run_repository.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| scripts/accept_m5_live.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| scripts/audit_m2.py | C | remove_from_current_tree | One-off acceptance/experiment tied to private historical run paths; not imported by retained runtime/tests. |
| scripts/audit_m3.py | C | remove_from_current_tree | One-off acceptance/experiment tied to private historical run paths; not imported by retained runtime/tests. |
| scripts/audit_m4.py | C | remove_from_current_tree | One-off acceptance/experiment tied to private historical run paths; not imported by retained runtime/tests. |
| scripts/audit_m5.py | C | remove_from_current_tree | One-off acceptance/experiment tied to private historical run paths; not imported by retained runtime/tests. |
| scripts/audit_m6.py | C | remove_from_current_tree | One-off acceptance/experiment tied to private historical run paths; not imported by retained runtime/tests. |
| scripts/audit_m6_sources.py | B | retain | Dotted import dependency from test_m6_source_campaign.py; source-time regression helper, retained. |
| scripts/audit_m7.py | C | remove_from_current_tree | One-off acceptance/experiment tied to private historical run paths; not imported by retained runtime/tests. |
| scripts/audit_research_intelligence.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| scripts/backup_m7.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| scripts/check_agent_infrastructure.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| scripts/compare_m6.py | C | remove_from_current_tree | Retired price-prediction/teaching/one-off experiment and its caller closure; no remaining runtime import. |
| scripts/complete_m1.sh | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| scripts/container_app.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| scripts/container_fixture.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| scripts/container_init.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| scripts/demo_evaluation.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| scripts/demo_harness.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| scripts/demo_memory_rsi.py | C | remove_from_current_tree | One-off acceptance/experiment tied to private historical run paths; not imported by retained runtime/tests. |
| scripts/evaluate_m6.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| scripts/export_release_examples.py | C | remove_from_current_tree | Retired v1.0 whitelist tar publisher/redacted report export and its obsolete fixed-asset tests; Core uses pyproject installation and dedicated current-tree/history audit. |
| scripts/local_app.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| scripts/maintain_m7.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| scripts/manage_candidates.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| scripts/measure_m5.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| scripts/offline_guard/sitecustomize.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| scripts/package_release.py | C | remove_from_current_tree | Retired v1.0 whitelist tar publisher/redacted report export and its obsolete fixed-asset tests; Core uses pyproject installation and dedicated current-tree/history audit. |
| scripts/preview_m6.py | C | remove_from_current_tree | One-off acceptance/experiment tied to private historical run paths; not imported by retained runtime/tests. |
| scripts/probe_model_stream.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| scripts/replicate_m6.py | C | remove_from_current_tree | Retired price-prediction/teaching/one-off experiment and its caller closure; no remaining runtime import. |
| scripts/restore_m7.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| scripts/run_independent_validation.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| scripts/run_quality_validation.py | C | remove_from_current_tree | One-off acceptance/experiment tied to private historical run paths; not imported by retained runtime/tests. |
| scripts/run_research_rsi.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| scripts/smoke_m7.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| sources/__init__.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| sources/sec.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| test_agent_harness.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| test_analysis_repair.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| test_chatgpt_auth.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| test_chatgpt_stream_contract.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| test_company_identity.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| test_evidence_repository.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| test_fetch_financials.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| test_filing_fact_audit.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| test_financial_facts.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| test_infrastructure_telemetry.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| test_lab_agent_batch.py | C | remove_from_current_tree | Retired price-prediction/teaching/one-off experiment and its caller closure; no remaining runtime import. |
| test_lab_core_boundaries.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| test_lab_evaluation.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| test_lab_price_experiment.py | C | remove_from_current_tree | Retired price-prediction/teaching/one-off experiment and its caller closure; no remaining runtime import. |
| test_m2_contracts.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| test_m3_application.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| test_m4_capabilities.py | C | remove_from_current_tree | Retired price-prediction/teaching/one-off experiment and its caller closure; no remaining runtime import. |
| test_m4_contracts.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| test_m4_evidence.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| test_m4_workflow.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| test_m5_api.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| test_m5_measurement.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| test_m5_plan_retry.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| test_m5_recovery.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| test_m5_repository.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| test_m6_comparison.py | C | remove_from_current_tree | Retired price-prediction/teaching/one-off experiment and its caller closure; no remaining runtime import. |
| test_m6_dataset_mode.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| test_m6_evaluations.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| test_m6_frozen_trial.py | C | remove_from_current_tree | Retired price-prediction/teaching/one-off experiment and its caller closure; no remaining runtime import. |
| test_m6_prompt_versions.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| test_m6_source_campaign.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| test_m7_delivery.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| test_m7_maintenance.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| test_m7_postgres_init.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| test_m7_release.py | C | remove_from_current_tree | Retired v1.0 whitelist tar publisher/redacted report export and its obsolete fixed-asset tests; Core uses pyproject installation and dedicated current-tree/history audit. |
| test_market_data.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| test_oracle_evidence_contract.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| test_point_in_time.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| test_prospective_snapshot.py | C | remove_from_current_tree | Retired price-prediction/teaching/one-off experiment and its caller closure; no remaining runtime import. |
| test_research_agent.py | C | remove_from_current_tree | Retired price-prediction/teaching/one-off experiment and its caller closure; no remaining runtime import. |
| test_research_intelligence.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| test_research_memory.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| test_research_pipeline.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| test_research_quality.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| test_research_rsi.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| test_research_sources.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| test_research_validation.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| test_research_workflow.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| test_sec_client.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| test_time_machine.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| test_yahoo_csv.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| tests/fixtures/m2_sec_annual_v1.json | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| tests/fixtures/m3_no_annual_coverage_v1.json | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| tests/fixtures/m4_duplicate_citation_v1.json | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| tests/fixtures/m5_recorded_crash_shapes_v1.json | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| tests/fixtures/m7_sources_v1.json | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| tests/fixtures/market_calendar_synthetic_v1.json | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| tests/fixtures/market_daily_synthetic_v1.csv | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| time_machine.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| yahoo_csv.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| .gitignore | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| README.md | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| bad_record.json | C | remove_from_current_tree | Retired price-prediction/teaching/one-off experiment and its caller closure; no remaining runtime import. |
| bad_syntax.json | C | remove_from_current_tree | Retired price-prediction/teaching/one-off experiment and its caller closure; no remaining runtime import. |
| data/filings.json | C | remove_from_current_tree | Retired price-prediction/teaching/one-off experiment and its caller closure; no remaining runtime import. |
| data/raw/aapl_submissions.json | C | remove_from_current_tree | Retired price-prediction/teaching/one-off experiment and its caller closure; no remaining runtime import. |
| data/raw/msft_submissions.json | C | remove_from_current_tree | Retired price-prediction/teaching/one-off experiment and its caller closure; no remaining runtime import. |
| data/snapshot_manifest.json | C | remove_from_current_tree | Retired price-prediction/teaching/one-off experiment and its caller closure; no remaining runtime import. |
| data_manifest.md | C | remove_from_current_tree | Retired price-prediction/teaching/one-off experiment and its caller closure; no remaining runtime import. |
| debug_case.py | C | remove_from_current_tree | Retired price-prediction/teaching/one-off experiment and its caller closure; no remaining runtime import. |
| domain.py | C | remove_from_current_tree | Retired price-prediction/teaching/one-off experiment and its caller closure; no remaining runtime import. |
| download_snapshots.py | C | remove_from_current_tree | Retired price-prediction/teaching/one-off experiment and its caller closure; no remaining runtime import. |
| empty.json | C | remove_from_current_tree | Retired price-prediction/teaching/one-off experiment and its caller closure; no remaining runtime import. |
| filing_parsing.py | C | remove_from_current_tree | Retired price-prediction/teaching/one-off experiment and its caller closure; no remaining runtime import. |
| models.py | B | retain | Import/test/entrypoint dependency or compatibility infrastructure. |
| parsing.py | C | remove_from_current_tree | Retired price-prediction/teaching/one-off experiment and its caller closure; no remaining runtime import. |
| practice.py | C | remove_from_current_tree | Retired price-prediction/teaching/one-off experiment and its caller closure; no remaining runtime import. |
| prepare_filings.py | C | remove_from_current_tree | Retired price-prediction/teaching/one-off experiment and its caller closure; no remaining runtime import. |
| real_sample.json | C | remove_from_current_tree | Retired price-prediction/teaching/one-off experiment and its caller closure; no remaining runtime import. |
| requirements.txt | F | retain | Documentation/operations history retained pending dedicated review; not deleted on age/name alone. |
| research.py | C | remove_from_current_tree | Retired price-prediction/teaching/one-off experiment and its caller closure; no remaining runtime import. |
| s04_debug_notes.md | C | remove_from_current_tree | Retired price-prediction/teaching/one-off experiment and its caller closure; no remaining runtime import. |
| sample.json | C | remove_from_current_tree | Retired price-prediction/teaching/one-off experiment and its caller closure; no remaining runtime import. |
| test_domain.py | C | remove_from_current_tree | Retired price-prediction/teaching/one-off experiment and its caller closure; no remaining runtime import. |
| test_filing_parsing.py | C | remove_from_current_tree | Retired price-prediction/teaching/one-off experiment and its caller closure; no remaining runtime import. |
| .env | E | leave_local_exclude_git | Local state/cache; not publishable; not cleaned. |
| .venv | E | leave_local_exclude_git | Local state/cache; not publishable; not cleaned. |
| data/live | E | leave_local_exclude_git | Local state/cache; not publishable; not cleaned. |
| .local-container | E | leave_local_exclude_git | Local state/cache; not publishable; not cleaned. |
| __pycache__ | D | leave_local_exclude_git | Local state/cache; not publishable; not cleaned. |
| .pytest_cache | D | leave_local_exclude_git | Local state/cache; not publishable; not cleaned. |
| MANIFEST.in | B | retain | New reviewed Core consolidation entrypoint, regression, dependency lock, security audit or documentation. |
| core_cli.py | A | retain | New reviewed Core consolidation entrypoint, regression, dependency lock, security audit or documentation. |
| diagnostics/event-0001.json | B | retain | New reviewed Core consolidation entrypoint, regression, dependency lock, security audit or documentation. |
| diagnostics/event-0002.json | B | retain | New reviewed Core consolidation entrypoint, regression, dependency lock, security audit or documentation. |
| diagnostics/event-0003.json | B | retain | New reviewed Core consolidation entrypoint, regression, dependency lock, security audit or documentation. |
| diagnostics/event-0004.json | B | retain | New reviewed Core consolidation entrypoint, regression, dependency lock, security audit or documentation. |
| diagnostics/event-0005.json | B | retain | New reviewed Core consolidation entrypoint, regression, dependency lock, security audit or documentation. |
| diagnostics/event-0006.json | B | retain | New reviewed Core consolidation entrypoint, regression, dependency lock, security audit or documentation. |
| diagnostics/event-0007.json | B | retain | New reviewed Core consolidation entrypoint, regression, dependency lock, security audit or documentation. |
| diagnostics/event-0008.json | B | retain | New reviewed Core consolidation entrypoint, regression, dependency lock, security audit or documentation. |
| diagnostics/event-0009.json | B | retain | New reviewed Core consolidation entrypoint, regression, dependency lock, security audit or documentation. |
| diagnostics/event-0010.json | B | retain | New reviewed Core consolidation entrypoint, regression, dependency lock, security audit or documentation. |
| diagnostics/event-0011.json | B | retain | New reviewed Core consolidation entrypoint, regression, dependency lock, security audit or documentation. |
| diagnostics/event-0012.json | B | retain | New reviewed Core consolidation entrypoint, regression, dependency lock, security audit or documentation. |
| diagnostics/event-0013.json | B | retain | New reviewed Core consolidation entrypoint, regression, dependency lock, security audit or documentation. |
| diagnostics/event-0014.json | B | retain | New reviewed Core consolidation entrypoint, regression, dependency lock, security audit or documentation. |
| docs/CORE_FILE_INVENTORY.json | B | retain | New reviewed Core consolidation entrypoint, regression, dependency lock, security audit or documentation. |
| docs/CORE_FILE_INVENTORY.md | B | retain | New reviewed Core consolidation entrypoint, regression, dependency lock, security audit or documentation. |
| pyproject.toml | B | retain | New reviewed Core consolidation entrypoint, regression, dependency lock, security audit or documentation. |
| requirements-core.txt | B | retain | New reviewed Core consolidation entrypoint, regression, dependency lock, security audit or documentation. |
| requirements-observability.txt | B | retain | New reviewed Core consolidation entrypoint, regression, dependency lock, security audit or documentation. |
| requirements.lock | B | retain | New reviewed Core consolidation entrypoint, regression, dependency lock, security audit or documentation. |
| scripts/check_core_release.py | B | retain | New reviewed Core consolidation entrypoint, regression, dependency lock, security audit or documentation. |
| scripts/core_smoke.py | A | retain | New reviewed Core consolidation entrypoint, regression, dependency lock, security audit or documentation. |
| test_core_release.py | B | retain | New reviewed Core consolidation entrypoint, regression, dependency lock, security audit or documentation. |
| test_core_runtime.py | B | retain | New reviewed Core consolidation entrypoint, regression, dependency lock, security audit or documentation. |
| config/quality_policy.example.json | B | retain | Reviewed minimal quality-policy example for the current Core research configuration. |
