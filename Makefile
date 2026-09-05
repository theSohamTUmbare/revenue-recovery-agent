PY ?= python
export PYTHONPATH := src

.PHONY: help demo test ablation sensitivity audit clean

help:
	@echo "make demo         - full benchmark, all three arms, writes reports/report.html"
	@echo "make test         - the guardrail and no-leak test suite"
	@echo "make ablation     - does the LLM beat deterministic rules? (add LLM=1 for the Claude arm)"
	@echo "make sensitivity  - re-run under +/-30% perturbation of the outcome table"
	@echo "make audit        - print sample audit entries, including the refusals"
	@echo ""
	@echo "No API key needed for any of these. With ANTHROPIC_API_KEY set,"
	@echo "the demo automatically uses Claude for diagnosis instead of the rules."

demo:
	$(PY) -m rre demo --progress

test:
	$(PY) -m pytest tests/ -q

ablation:
ifdef LLM
	$(PY) -m rre ablation --llm
else
	$(PY) -m rre ablation
endif

sensitivity:
	$(PY) -m rre sensitivity --trials 10

audit:
	$(PY) -m rre audit -n 2

clean:
	rm -rf reports/*.html reports/*.jsonl .pytest_cache
	find . -name __pycache__ -type d -exec rm -rf {} +
