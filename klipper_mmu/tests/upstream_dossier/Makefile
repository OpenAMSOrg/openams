# SPDX-License-Identifier: GPL-3.0-or-later
# Happy Hare dossier tool (build/b2/review_oracle_plan.md D1): trace boxturtle
# and emu into a scratch directory, check the three committed docs/devices/
# dossiers against a fresh re-trace, run tools/afc_facts.py compare for all
# three devices against the pinned AFC templates (every `differ` row must be
# decided, exit 0), and run Happy Hare's own test.test_mmu_motion as the
# health check (if that goes red, no dossier from this tree is trusted).
# The AFC compare step only needs the pinned AFC archive (extracted on demand
# from references/, no network) and plain system Python; the HH-dependent
# steps below it are optional and skip when HH_PYTHON is missing (needs the
# extracted HH archive and a Python with greenlet+jinja2).
PRODUCT_ROOT ?= ../..
BUILD_DIR ?= build

# Shared build inputs (extracted archives, the integration klippy venv) live
# under this package's own build/, or wherever KLIPPER_MMU_BUILD_ROOT points.
SHARED_BUILD ?= $(if $(KLIPPER_MMU_BUILD_ROOT),$(KLIPPER_MMU_BUILD_ROOT),$(PRODUCT_ROOT)/build)

HH_ROOT ?= $(SHARED_BUILD)/upstream/happy-hare-c54c736
HH_PYTHON ?= $(SHARED_BUILD)/integration-env/klippy/bin/python

AFC_ARCHIVE := $(PRODUCT_ROOT)/references/AFC-Klipper-Add-On-5ea84ea6fab75b9b49db090f309154d1e52272bb.tar.gz
AFC_ROOT ?= $(SHARED_BUILD)/upstream/afc-5ea84ea

.PHONY: test extract extract-afc

extract:
	$(MAKE) -C $(PRODUCT_ROOT)/tests/hh_harness extract HH_ROOT=$(HH_ROOT)

# The AFC tarball has no single wrapping directory (unlike Happy Hare's), so
# it extracts straight into AFC_ROOT -- see tools/afc_facts.py's docstring.
extract-afc:
	@if [ ! -d "$(AFC_ROOT)" ]; then \
		echo "extracting $(AFC_ARCHIVE) -> $(AFC_ROOT)"; \
		mkdir -p "$(AFC_ROOT)"; \
		tar -xzf "$(AFC_ARCHIVE)" -C "$(AFC_ROOT)"; \
	fi

test: extract extract-afc
	@echo "== AFC facts compare: boxturtle, nightowl, emu (every differ decided) =="
	timeout 20s python3 -B $(PRODUCT_ROOT)/tools/afc_facts.py compare \
		$(PRODUCT_ROOT)/docs/devices/boxturtle.json $(AFC_ROOT)/templates/AFC_Turtle_1.cfg \
		--mcu $(AFC_ROOT)/config/mcu/AFC_Lite.cfg
	timeout 20s python3 -B $(PRODUCT_ROOT)/tools/afc_facts.py compare \
		$(PRODUCT_ROOT)/docs/devices/nightowl.json $(AFC_ROOT)/templates/AFC_NightOwl_1.cfg \
		--mcu $(AFC_ROOT)/config/mcu/ERB_2.0.cfg
	timeout 20s python3 -B $(PRODUCT_ROOT)/tools/afc_facts.py compare \
		$(PRODUCT_ROOT)/docs/devices/emu.json $(AFC_ROOT)/templates/EMU.cfg \
		--mcu $(AFC_ROOT)/config/mcu/EMU_SLB.cfg
	@if [ ! -x "$(HH_PYTHON)" ]; then \
		echo "SKIP: Happy Hare dossier trace/check needs the extracted archive and a Python with greenlet+jinja2 (missing HH_PYTHON=$(HH_PYTHON))"; \
		exit 0; \
	fi
	@echo "== trace boxturtle + emu into a scratch dir =="
	@mkdir -p $(BUILD_DIR)
	timeout 60s env HH_ROOT=$(HH_ROOT) HH_PYTHON=$(HH_PYTHON) \
		python3 -B $(PRODUCT_ROOT)/tools/upstream_dossier.py trace boxturtle \
		--out $(BUILD_DIR)/boxturtle.json
	timeout 60s env HH_ROOT=$(HH_ROOT) HH_PYTHON=$(HH_PYTHON) \
		python3 -B $(PRODUCT_ROOT)/tools/upstream_dossier.py trace emu \
		--out $(BUILD_DIR)/emu.json
	@echo "== check the committed dossiers against a fresh re-trace =="
	timeout 60s env HH_ROOT=$(HH_ROOT) HH_PYTHON=$(HH_PYTHON) \
		python3 -B $(PRODUCT_ROOT)/tools/upstream_dossier.py check
	@echo "== Happy Hare's own health check (test.test_mmu_motion) =="
	( cd "$(HH_ROOT)" && timeout 60s $(HH_PYTHON) -m unittest test.test_mmu_motion -v )

clean:
	rm -rf $(BUILD_DIR)
