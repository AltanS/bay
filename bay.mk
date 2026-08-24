# Bay Framework — Makefile aliases (backwards compatibility)
# These delegate to the Python CLI via bin/bay.
# Primary interface: bin/bay <command>

# Capture environment from positional argument: make bay:deploy production
_ENV := $(word 2,$(MAKECMDGOALS))

ifneq ($(_ENV),)
  $(eval $(_ENV):;@:)
endif

bay\:help:
	@bin/bay --help

bay\:install:
	@bin/bay install

bay\:update:
	@bin/bay update

bay\:status:
	@bin/bay status

bay\:provision:
	@bin/bay provision $(_ENV) $(if $(tags),--tags $(tags)) $(ARGS)

bay\:deploy:
	@bin/bay deploy $(_ENV) $(if $(tags),--tags $(tags)) $(ARGS)

bay\:restore:
	@bin/bay restore $(_ENV) $(if $(tags),--tags $(tags)) $(ARGS)

bay\:vault-edit:
	@bin/bay vault edit $(_ENV) $(if $(file),--file $(file))

bay\:vault-view:
	@bin/bay vault view $(_ENV) $(if $(file),--file $(file))

bay\:vault-encrypt:
	@bin/bay vault encrypt $(_ENV) $(if $(file),--file $(file))

bay\:vault-decrypt:
	@bin/bay vault decrypt $(_ENV) $(if $(file),--file $(file))

bay\:secret:
	@bin/bay secret $(if $(hash),--hash $(hash))

bay\:test:
	@bin/bay test

.PHONY: bay\:help bay\:install bay\:update bay\:status bay\:provision bay\:deploy bay\:restore bay\:vault-edit bay\:vault-view bay\:vault-encrypt bay\:vault-decrypt bay\:secret bay\:test
