IMAGE ?= blast-radius-entry
SAMPLE := $(CURDIR)/sample
OUTPUT := $(CURDIR)/output.jsonl

# The flags the scorer runs your image with. The scorer is linux/amd64, so on
# an Apple Silicon Mac --platform makes your local run match it.
RUN_FLAGS := --rm --platform linux/amd64 --network none --cpus 2 \
	--memory 4096m --memory-swap 4096m --pids-limit 256 --read-only \
	--tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL \
	--security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp \
	--volume $(SAMPLE)/commits:/data/commits:ro \
	--volume $(SAMPLE)/tests.json:/data/tests.json:ro

.PHONY: build sample check maps

build:
	docker build --platform linux/amd64 --tag $(IMAGE) .

# Runs your image once on the sample commits under the scorer's limits and
# scores what it printed.
sample: build
	docker run $(RUN_FLAGS) $(IMAGE) > $(OUTPUT)
	python3 score.py $(SAMPLE) $(SAMPLE)/answers.json $(OUTPUT)

# The output's rules only: every line well formed and every commit answered.
# A wrong selection still passes here; make sample counts the misses.
check: build
	docker run $(RUN_FLAGS) $(IMAGE) > $(OUTPUT)
	python3 score.py $(SAMPLE) $(SAMPLE)/answers.json $(OUTPUT) > /dev/null && echo "format ok"

# Rebuilds maps.tar.xz from the pinned sqlglot commit: every test is run once,
# alone, under tracing, then timed untraced; expensive tests are then run once
# more per group of independent statements. Takes a quarter of an hour, and wants
# a quiet machine: the timings decide when verifying beats scheduling. Build on
# the machine's own architecture (duckdb misbehaves under amd64 emulation on
# Apple Silicon); the maps hold only line numbers, hashes and timings, which do
# not depend on it.
maps:
	docker build --target base --tag $(IMAGE)-base .
	rm -rf $(CURDIR)/work/maps && mkdir -p $(CURDIR)/work/maps
	docker run --rm --volume $(CURDIR):/repo --env PYTHONPATH=/repo --workdir /sg $(IMAGE)-base sh -c "\
		python -m br.tracer /sg /repo/work/maps 4 && \
		python -m br.aggregate /sg /repo/work/maps && \
		python -m br.timing /sg /repo/work/maps 2 && \
		python -m br.timing --bare-all /sg /repo/work/maps && \
		python -m br.fixtures /sg /repo/work/maps && \
		python -m br.basestate /sg /repo/work/maps && \
		python -m br.stmts /sg /repo/work/maps 4 && \
		python -m br.heavy /sg /repo/work/maps 2 && \
		python -m br.setups /sg /repo/work/maps && \
		python -m br.modinfo /sg /repo/work/maps && \
		python -m br.entries /sg /repo/unit_entries.json && \
		python -m br.stages /sg /repo/stage_prints.json && \
		rm -rf /repo/work/maps/parts /repo/work/maps/stmt_parts && \
		cd /repo/work/maps && python -m tarfile -c /repo/maps.tar.xz ."
