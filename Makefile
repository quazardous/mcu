# SPDX-License-Identifier: MIT
# mcu: install on a dedicated box, build a release, run the checks.
#   make install          the files (box/install.list, the dashboard, mcu-job); then: sudo box/mcu-admin init
#   make dist             mcu-<version>.tar.gz, the dashboard built in (the box needs no node)
#   make check            the unit tests and the tracker-reference check
#   make lint             ruff (Python) and shellcheck (shell), through uvx when they are not installed
#   make release VERSION=x.y.z   checks, sets VERSION, commits and tags vx.y.z (pushing stays yours)
# DESTDIR stages an install elsewhere (a package build).

VERSION := $(shell cat VERSION 2>/dev/null || echo 0.0.0)
DIST := mcu-$(VERSION)

.PHONY: install dashboard dist check lint release clean

PYFILES := box/mcu.py box/api.py box/collect.py box/mcuconf.py box/mcu-helper box/mcu-task box/hw \
	client/mcuctl client/mcu tests scripts/check-refs
SHFILES := box/mcu-admin box/install-files client/remote.sh .githooks/pre-commit .githooks/commit-msg
RUFF := $(shell command -v ruff 2>/dev/null || echo uvx ruff)
SHELLCHECK := $(shell command -v shellcheck 2>/dev/null || echo uvx --from shellcheck-py shellcheck)

install:
	DESTDIR=$(DESTDIR) box/install-files

dashboard:
	cd dashboard && npm ci && npm run build

dist: dashboard
	rm -rf build/$(DIST) && mkdir -p build/$(DIST)
	git ls-files -z | xargs -0 cp --parents -t build/$(DIST)
	mkdir -p build/$(DIST)/dashboard && cp -r dashboard/dist build/$(DIST)/dashboard/dist
	tar -C build -czf $(DIST).tar.gz $(DIST)
	@echo "$(DIST).tar.gz"

check:
	python3 -m unittest discover tests
	scripts/check-refs

lint:
	$(RUFF) check $(PYFILES)
	$(SHELLCHECK) -S warning $(SHFILES)

release:
	@test -n "$(VERSION)" || { echo "make release VERSION=x.y.z"; exit 2; }
	@echo "$(VERSION)" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$$' || { echo "VERSION: x.y.z"; exit 2; }
	@test -z "$$(git status --porcelain --untracked-files=no)" || { echo "commit your changes first"; exit 2; }
	@! git rev-parse -q --verify "refs/tags/v$(VERSION)" > /dev/null || { echo "v$(VERSION) exists"; exit 2; }
	@grep -q '^## \[$(VERSION)\]' CHANGELOG.md || { echo "CHANGELOG.md has no [$(VERSION)] section"; exit 2; }
	$(MAKE) check
	echo "$(VERSION)" > VERSION
	git commit -q -m "release $(VERSION)" VERSION
	git tag -a "v$(VERSION)" -m "mcu $(VERSION)"
	@echo "tagged v$(VERSION): git push origin main v$(VERSION) publishes it"

clean:
	rm -rf build mcu-*.tar.gz
