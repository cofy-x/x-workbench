SHELL := /bin/bash

TOOLS_ROOT := tools
TOOL ?= logo_generator
TOOL_DIR := $(TOOLS_ROOT)/$(TOOL)
TOOL_APP := $(TOOL_DIR)/app.py
TOOL_WEB := $(TOOL_DIR)/web/index.html
TOOL_LIST := $(shell find $(TOOLS_ROOT) -mindepth 2 -maxdepth 2 -name app.py -type f 2>/dev/null | awk -F/ '{print $$2}' | sort)

HOST ?= 127.0.0.1
PORT ?= 8000
BRAND ?= Avant
VARIANT ?= v3_forward
ICON_MODE ?= brand_seeded
GENERATE_ALL ?= 0
ARGS ?=
DOCKER_IMAGE ?= x-workbench:latest
DOCKER_CONTAINER ?= x-workbench
DOCKER_PORT ?= 8080
DOCKER_HF_VOLUME ?= x-workbench-whisper-cache

.DEFAULT_GOAL := help

.PHONY: help list-tools guard-tool serve generate run check check-all docker-build docker-run docker-stop docker-logs clean-generated

help:
	@echo "Available targets:"
	@echo "  make list-tools                                  # List available tools"
	@echo "  make serve TOOL=logo_generator                   # Start selected tool server"
	@echo "  make generate TOOL=logo_generator BRAND=Avant    # Generate assets once (logo_generator only)"
	@echo "  make run TOOL=video_kit ARGS='--process ...'     # Run arbitrary CLI args for a tool"
	@echo "  make check TOOL=logo_generator                   # Syntax check selected tool"
	@echo "  make check-all                                   # Syntax check all tools"
	@echo "  make docker-build                                # Build all-in-one Docker image"
	@echo "  make docker-run                                  # Run Docker gateway + all tools"
	@echo "  make docker-stop                                 # Stop and remove Docker container"
	@echo "  make docker-logs                                 # Follow container logs"
	@echo "  make clean-generated                             # Remove generated assets"
	@echo ""
	@echo "Variables:"
	@echo "  TOOL=$(TOOL)"
	@echo "  HOST=$(HOST) PORT=$(PORT)"
	@echo "  BRAND=$(BRAND) VARIANT=$(VARIANT) ICON_MODE=$(ICON_MODE)"
	@echo "  GENERATE_ALL=$(GENERATE_ALL)  # 1 to add --generate-all-variants"
	@echo "  ARGS=$(ARGS)"
	@echo "  DOCKER_IMAGE=$(DOCKER_IMAGE) DOCKER_CONTAINER=$(DOCKER_CONTAINER)"
	@echo "  DOCKER_PORT=$(DOCKER_PORT) DOCKER_HF_VOLUME=$(DOCKER_HF_VOLUME)"
	@echo ""
	@echo "Discovered tools: $(TOOL_LIST)"

list-tools:
	@echo "$(TOOL_LIST)" | tr ' ' '\n' | sed '/^$$/d'

guard-tool:
	@if [ ! -f "$(TOOL_APP)" ]; then \
		echo "Error: tool '$(TOOL)' not found at $(TOOL_APP)"; \
		echo "Hint: run 'make list-tools'"; \
		exit 1; \
	fi

serve: guard-tool
	uv run python $(TOOL_APP) --host $(HOST) --port $(PORT)

generate: guard-tool
	uv run python $(TOOL_APP) --generate --brand-name "$(BRAND)" --default-variant "$(VARIANT)" --icon-mode "$(ICON_MODE)" $(if $(filter 1 true TRUE yes YES,$(GENERATE_ALL)),--generate-all-variants,)

run: guard-tool
	uv run python $(TOOL_APP) $(ARGS)

check: guard-tool
	python3 -m py_compile $(TOOL_APP)

check-all:
	@set -e; \
	if [ -z "$(TOOL_LIST)" ]; then \
		echo "No tools found under $(TOOLS_ROOT)"; \
		exit 1; \
	fi; \
	for tool in $(TOOL_LIST); do \
		echo "[check] $$tool"; \
		python3 -m py_compile "$(TOOLS_ROOT)/$$tool/app.py"; \
	done

docker-build:
	docker build -t $(DOCKER_IMAGE) .

docker-run:
	@mkdir -p generated
	@docker rm -f $(DOCKER_CONTAINER) >/dev/null 2>&1 || true
	docker run -d --name $(DOCKER_CONTAINER) \
		-p $(DOCKER_PORT):8080 \
		-v "$(PWD)/generated:/app/generated" \
		-v "$(DOCKER_HF_VOLUME):/data/hf" \
		$(DOCKER_IMAGE)
	@echo "Gateway: http://127.0.0.1:$(DOCKER_PORT)"

docker-stop:
	@docker rm -f $(DOCKER_CONTAINER) >/dev/null 2>&1 || true

docker-logs:
	docker logs -f $(DOCKER_CONTAINER)

clean-generated:
	rm -rf generated/*
