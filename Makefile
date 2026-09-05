PY ?= python
export PYTHONPATH := src

.PHONY: help demo test ablation freetext sensitivity audit clean

help:
	@echo "make demo         - full benchmark, all three arms, writes reports/report.html"
	@echo "make test         - the guardrail and no-leak test suite"
	@echo "make ablation     - does the LLM beat deterministic rules? (add LLM=1)"
	@echo "make freetext     - intent extraction from customer prose (add LLM=1)"
	@echo "make sensitivity  - re-run under +/-30% perturbation of the outcome table"
	@echo "make audit        - print sample audit entries, including the refusals"
	@echo ""
	@echo "No API key needed for any of these. With GEMINI_API_KEY (or"
	@echo "ANTHROPIC_API_KEY) set, diagnosis runs through a model instead."

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

freetext:
ifdef LLM
	$(PY) -m rre freetext --llm
else
	$(PY) -m rre freetext
endif

sensitivity:
	$(PY) -m rre sensitivity --trials 10

audit:
	$(PY) -m rre audit -n 2

clean:
	rm -rf reports/*.html reports/*.jsonl .pytest_cache
	find . -name __pycache__ -type d -exec rm -rf {} +
