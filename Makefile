# Top-level entry points for both halves of this repository: the openams
# Klipper host plugin (test/) and the klipper_mmu MCU add-on (klipper_mmu/).
# The package keeps its own `make check`; this only gives one place to run
# everything, which is also what CI does.

.PHONY: test test-openams test-mmu test-binding

test: test-openams test-mmu test-binding

test-openams:
	python3 -m unittest discover -s test

test-mmu:
	$(MAKE) -C klipper_mmu check

# The binding suite runs klipper_mmu's tests against the openams sources in
# this repository (klipper_mmu/ sits inside it, so no OPENAMS_SOURCE is needed).
test-binding:
	$(MAKE) -C klipper_mmu check-openams
